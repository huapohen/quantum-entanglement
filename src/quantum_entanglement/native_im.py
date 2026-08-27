# ruff: noqa: UP006, UP035
"""Executable provider-neutral value contract for native IM V1.

This module contains immutable wire values only.  A codec-valid value is not authenticated,
durable, authorized, or permitted to perform an external effect.  Provider adapters and
composition roots must establish those facts at their dedicated boundaries.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Tuple, Type, TypeVar, cast

from ._native_im_codec import (
    NATIVE_IM_SCHEMA_VERSION,
    NativeIMCodecTooLargeError,
    _boolean,
    _canonical_json_bytes,
    _decode_json_bytes,
    _digest,
    _display_text,
    _enum,
    _id,
    _media_type,
    _message_text,
    _model_digest,
    _non_negative_integer,
    _ordered_unique_text,
    _plain_dict,
    _plain_list,
    _positive_integer,
    _schema_version,
    _timestamp,
    _traceparent,
)

_MAX_REFERENCE_BYTES = 3 * 1_024 * 1_024
_MAX_MESSAGE_CONTENT_BYTES = 2 * 1_024 * 1_024
_MAX_ROLE_IDS = 1_024
_MAX_MESSAGE_SEGMENTS = 4_096
_MAX_ATTACHMENTS = 64
_MAX_INBOUND_ENVELOPES = 1_000
_MAX_INBOUND_PAGE_BYTES = 16 * 1_024 * 1_024
_MAX_ACTION_BYTES = 3 * 1_024 * 1_024
_MAX_RECEIPT_BYTES = 256 * 1_024

_PARTICIPANT_KINDS = {"human", "agent", "service"}
_SEGMENT_KINDS = {"text", "mention"}
_MEMBERSHIP_CHANGE_KINDS = {"joined", "left", "role_changed", "suspended", "restored"}
_INBOUND_EVENT_TYPES = {
    "message.created",
    "message.edited",
    "message.deleted",
    "reaction.added",
    "reaction.removed",
    "membership.changed",
}
_OPERATIONS = {
    "send_message",
    "edit_message",
    "delete_message",
    "add_reaction",
    "remove_reaction",
}
_LOOKUP_MODES = {"idempotency_key", "provider_operation_id"}
_NEGATIVE_ACCEPTANCE_MODES = {"authoritative_terminal", "unavailable"}
_REVISION_MODES = {"not_applicable", "required_cas", "provider_best_effort"}
_IDEMPOTENCY_MODES = {"receiver_deduplicated", "not_supported"}
_RECEIPT_STATES = {
    "succeeded",
    "rejected",
    "retryable_not_accepted",
    "effect_unknown",
    "reconciled_succeeded",
    "reconciled_rejected",
}
_DISPATCH_RECEIPT_STATES = {
    "succeeded",
    "rejected",
    "retryable_not_accepted",
    "effect_unknown",
}
_TERMINAL_ERROR_CODES = {
    "terminal_permission_denied",
    "terminal_invalid_target",
    "terminal_revision_conflict",
    "terminal_unsupported",
    "terminal_not_accepted",
}
_TRANSIENT_NOT_ACCEPTED_ERROR_CODES = {
    "rate_limited_not_accepted",
    "temporarily_unavailable_not_accepted",
}
_UNKNOWN_ERROR_CODES = {
    "delivery_outcome_unknown",
    "acceptance_not_final",
    "acceptance_retention_expired",
}
_RECEIPT_ERROR_CODES = (
    _TERMINAL_ERROR_CODES | _TRANSIENT_NOT_ACCEPTED_ERROR_CODES | _UNKNOWN_ERROR_CODES
)

_CONVERSATION_FIELDS = {
    "schemaVersion",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "conversationId",
    "threadId",
}
_PARTICIPANT_FIELDS = {
    "schemaVersion",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "participantId",
    "participantKind",
    "displayName",
    "roleIds",
    "membershipRevision",
}
_ATTACHMENT_FIELDS = {
    "schemaVersion",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "attachmentId",
    "version",
    "mediaType",
    "byteSize",
    "sha256",
    "immutableRef",
}
_MESSAGE_SEGMENT_FIELDS = {"schemaVersion", "kind", "text", "participantId"}
_MESSAGE_CONTENT_FIELDS = {"schemaVersion", "segments", "attachments"}
_MESSAGE_REF_FIELDS = {"schemaVersion", "conversation", "messageId", "revision", "createdAt"}
_REACTION_REF_FIELDS = {
    "schemaVersion",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "reactionKey",
}
_MEMBERSHIP_CHANGE_FIELDS = {
    "schemaVersion",
    "subject",
    "changeKind",
    "previousMembershipRevision",
}
_INBOUND_EVENT_FIELDS = {
    "schemaVersion",
    "eventId",
    "eventType",
    "cursor",
    "sequenceNumber",
    "conversation",
    "message",
    "sender",
    "content",
    "reaction",
    "membershipChange",
    "occurredAt",
    "firstReceivedAt",
    "ingressRequestId",
    "correlationId",
    "causationId",
    "transportEvidenceDigest",
}
_VERIFIED_ENVELOPE_FIELDS = {
    "schemaVersion",
    "event",
    "eventDigest",
    "verificationId",
    "verifierId",
    "authenticationEvidenceDigest",
    "tenantMappingRevision",
    "verifiedAt",
    "traceparent",
}
_CAPABILITY_REQUEST_FIELDS = {
    "schemaVersion",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "requestId",
}
_ACCEPTANCE_LOOKUP_FIELDS = {
    "schemaVersion",
    "lookupMode",
    "negativeAcceptanceMode",
    "retentionSeconds",
    "consistencySeconds",
}
_OPERATION_CAPABILITY_FIELDS = {
    "schemaVersion",
    "operation",
    "revisionMode",
    "idempotencyMode",
    "acceptanceLookups",
}
_CAPABILITY_SNAPSHOT_FIELDS = {
    "schemaVersion",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "revision",
    "observedAt",
    "operations",
    "idempotencyRetentionSeconds",
    "supportsThreads",
    "supportsMentions",
    "supportsAttachments",
    "supportsMembershipEvents",
    "maxTextBytes",
    "maxAttachments",
    "maxAttachmentBytes",
}
_INBOUND_READ_REQUEST_FIELDS = {
    "schemaVersion",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "afterCursor",
    "afterSequence",
    "snapshotToken",
    "limit",
    "readRequestId",
}
_INBOUND_PAGE_FIELDS = {
    "schemaVersion",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "readRequestId",
    "readRequestDigest",
    "snapshotToken",
    "envelopes",
    "nextCursor",
    "nextSequence",
    "hasMore",
    "capabilityRevision",
    "capabilityDigest",
}
_ACTION_INTENT_FIELDS = {
    "schemaVersion",
    "actionId",
    "tenantId",
    "workspaceId",
    "actorId",
    "delegatorId",
    "conversation",
    "operation",
    "targetMessage",
    "content",
    "reaction",
    "createdAt",
    "correlationId",
    "causationId",
    "traceparent",
}
_ACTION_COMMAND_FIELDS = {
    "schemaVersion",
    "commandId",
    "intent",
    "intentDigest",
    "idempotencyKey",
    "authorizationDecisionId",
    "authorizationRevision",
    "approvalDecisionId",
    "approvalRevision",
    "policyRevision",
    "capabilityRevision",
    "capabilityDigest",
    "authorizedAt",
    "expiresAt",
    "correlationId",
    "causationId",
    "traceparent",
}
_DISPATCH_REQUEST_FIELDS = {
    "schemaVersion",
    "dispatchAttemptId",
    "command",
    "commandDigest",
    "attemptNumber",
    "fenceId",
    "fenceRevision",
    "claimedAt",
    "dispatchDeadlineAt",
    "correlationId",
    "causationId",
    "traceparent",
}
_ACTION_RECEIPT_FIELDS = {
    "schemaVersion",
    "receiptId",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "actionId",
    "commandId",
    "dispatchAttemptId",
    "dispatchRequestDigest",
    "intentDigest",
    "commandDigest",
    "idempotencyKey",
    "attemptNumber",
    "state",
    "providerOperationId",
    "providerMessage",
    "receiverEvidenceDigest",
    "errorCode",
    "retryAfterSeconds",
    "observedAt",
    "correlationId",
    "causationId",
    "traceparent",
}

_WireT = TypeVar("_WireT", bound="_NativeIMWireValue")


class _NativeIMWireValue:
    _MODEL_NAME: ClassVar[str]
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_REFERENCE_BYTES

    def to_dict(self) -> Dict[str, Any]:  # pragma: no cover - abstract boundary.
        raise NotImplementedError

    @classmethod
    def from_dict(cls: Type[_WireT], value: object) -> _WireT:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def from_json_bytes(cls: Type[_WireT], encoded: object) -> _WireT:
        decoded = _decode_json_bytes(
            encoded,
            cls._MODEL_NAME,
            maximum_bytes=cls._MAX_CANONICAL_BYTES,
        )
        return cls.from_dict(decoded)

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > self._MAX_CANONICAL_BYTES:
            raise NativeIMCodecTooLargeError(f"{self._MODEL_NAME} exceeds its canonical byte limit")
        return encoded

    def canonical_digest(self) -> str:
        self.canonical_bytes()
        return _model_digest(self._MODEL_NAME, self.to_dict())


def _optional_id(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _id(value, label)


def _optional_display_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _display_text(value, label)


def _optional_traceparent(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _traceparent(value, label)


def _require_exact_model(value: object, expected: type, label: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{label} must use the exact V1 model class")


def _require_exact_tuple(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    return value


def _scope(value: Any) -> Tuple[str, str, str, str]:
    return (value.tenant_id, value.workspace_id, value.provider, value.channel_id)


@dataclass(frozen=True)
class IMConversationRefV1(_NativeIMWireValue):
    schema_version: int
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    conversation_id: str
    thread_id: str | None

    _MODEL_NAME: ClassVar[str] = "IMConversationRefV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMConversationRefV1, "conversation")
        _schema_version(self.schema_version)
        for value, label in (
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.conversation_id, "conversationId"),
        ):
            _id(value, label)
        _optional_id(self.thread_id, "threadId")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "provider": self.provider,
            "channelId": self.channel_id,
            "conversationId": self.conversation_id,
            "threadId": self.thread_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMConversationRefV1:
        if cls is not IMConversationRefV1:
            raise TypeError("conversation decoder requires the exact V1 class")
        body = _plain_dict(value, _CONVERSATION_FIELDS, "conversation")
        return cls(
            schema_version=body["schemaVersion"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            conversation_id=body["conversationId"],
            thread_id=body["threadId"],
        )


@dataclass(frozen=True)
class IMParticipantRefV1(_NativeIMWireValue):
    schema_version: int
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    participant_id: str
    participant_kind: str
    display_name: str | None = field(repr=False)
    role_ids: Tuple[str, ...]
    membership_revision: str

    _MODEL_NAME: ClassVar[str] = "IMParticipantRefV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMParticipantRefV1, "participant")
        _schema_version(self.schema_version)
        for value, label in (
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.participant_id, "participantId"),
            (self.membership_revision, "membershipRevision"),
        ):
            _id(value, label)
        _enum(self.participant_kind, _PARTICIPANT_KINDS, "participantKind")
        _optional_display_text(self.display_name, "displayName")
        _require_exact_tuple(self.role_ids, "roleIds")
        roles = self.role_ids
        if len(roles) > _MAX_ROLE_IDS:
            raise NativeIMCodecTooLargeError("roleIds exceeds its item limit")
        validated = tuple(_id(role, f"roleIds[{index}]") for index, role in enumerate(roles))
        _ordered_unique_text(validated, "roleIds")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "provider": self.provider,
            "channelId": self.channel_id,
            "participantId": self.participant_id,
            "participantKind": self.participant_kind,
            "displayName": self.display_name,
            "roleIds": list(self.role_ids),
            "membershipRevision": self.membership_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMParticipantRefV1:
        if cls is not IMParticipantRefV1:
            raise TypeError("participant decoder requires the exact V1 class")
        body = _plain_dict(value, _PARTICIPANT_FIELDS, "participant")
        roles = _plain_list(body["roleIds"], "roleIds", maximum_items=_MAX_ROLE_IDS)
        return cls(
            schema_version=body["schemaVersion"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            participant_id=body["participantId"],
            participant_kind=body["participantKind"],
            display_name=body["displayName"],
            role_ids=tuple(cast(list[str], roles)),
            membership_revision=body["membershipRevision"],
        )


@dataclass(frozen=True)
class IMAttachmentRefV1(_NativeIMWireValue):
    schema_version: int
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    attachment_id: str
    version: str
    media_type: str
    byte_size: int
    sha256: str
    immutable_ref: str = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMAttachmentRefV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMAttachmentRefV1, "attachment")
        _schema_version(self.schema_version)
        for value, label in (
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.attachment_id, "attachmentId"),
            (self.version, "version"),
            (self.immutable_ref, "immutableRef"),
        ):
            _id(value, label)
        _media_type(self.media_type, "mediaType")
        _non_negative_integer(self.byte_size, "byteSize")
        _digest(self.sha256, "sha256")
        if "://" in self.immutable_ref or self.immutable_ref.startswith("//"):
            raise ValueError("immutableRef must be an opaque identity, not a URL")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "provider": self.provider,
            "channelId": self.channel_id,
            "attachmentId": self.attachment_id,
            "version": self.version,
            "mediaType": self.media_type,
            "byteSize": self.byte_size,
            "sha256": self.sha256,
            "immutableRef": self.immutable_ref,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMAttachmentRefV1:
        if cls is not IMAttachmentRefV1:
            raise TypeError("attachment decoder requires the exact V1 class")
        body = _plain_dict(value, _ATTACHMENT_FIELDS, "attachment")
        return cls(
            schema_version=body["schemaVersion"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            attachment_id=body["attachmentId"],
            version=body["version"],
            media_type=body["mediaType"],
            byte_size=body["byteSize"],
            sha256=body["sha256"],
            immutable_ref=body["immutableRef"],
        )


@dataclass(frozen=True)
class IMMessageSegmentV1(_NativeIMWireValue):
    schema_version: int
    kind: str
    text: str | None = field(repr=False)
    participant_id: str | None

    _MODEL_NAME: ClassVar[str] = "IMMessageSegmentV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMMessageSegmentV1, "message segment")
        _schema_version(self.schema_version)
        _enum(self.kind, _SEGMENT_KINDS, "kind")
        if self.kind == "text":
            if self.text is None or self.participant_id is not None:
                raise ValueError("text segment fields do not match the V1 matrix")
            _message_text(self.text, "text", allow_empty=False)
        elif self.text is not None or self.participant_id is None:
            raise ValueError("mention segment fields do not match the V1 matrix")
        else:
            _id(self.participant_id, "participantId")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "kind": self.kind,
            "text": self.text,
            "participantId": self.participant_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMMessageSegmentV1:
        if cls is not IMMessageSegmentV1:
            raise TypeError("message segment decoder requires the exact V1 class")
        body = _plain_dict(value, _MESSAGE_SEGMENT_FIELDS, "message segment")
        return cls(
            schema_version=body["schemaVersion"],
            kind=body["kind"],
            text=body["text"],
            participant_id=body["participantId"],
        )


@dataclass(frozen=True)
class IMMessageContentV1(_NativeIMWireValue):
    schema_version: int
    segments: Tuple[IMMessageSegmentV1, ...] = field(repr=False)
    attachments: Tuple[IMAttachmentRefV1, ...] = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMMessageContentV1"
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_MESSAGE_CONTENT_BYTES

    def __post_init__(self) -> None:
        _require_exact_model(self, IMMessageContentV1, "message content")
        _schema_version(self.schema_version)
        _require_exact_tuple(self.segments, "segments")
        _require_exact_tuple(self.attachments, "attachments")
        segments = self.segments
        attachments = self.attachments
        if len(segments) > _MAX_MESSAGE_SEGMENTS:
            raise NativeIMCodecTooLargeError("segments exceeds its item limit")
        if len(attachments) > _MAX_ATTACHMENTS:
            raise NativeIMCodecTooLargeError("attachments exceeds its item limit")
        if not segments and not attachments:
            raise ValueError("message content must contain a segment or attachment")
        for index, segment in enumerate(segments):
            _require_exact_model(segment, IMMessageSegmentV1, f"segments[{index}]")
            if index and segments[index - 1].kind == "text" and segment.kind == "text":
                raise ValueError("adjacent text segments are not canonical")
        for index, attachment in enumerate(attachments):
            _require_exact_model(attachment, IMAttachmentRefV1, f"attachments[{index}]")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "segments": [segment.to_dict() for segment in self.segments],
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }

    @classmethod
    def from_dict(cls, value: object) -> IMMessageContentV1:
        if cls is not IMMessageContentV1:
            raise TypeError("message content decoder requires the exact V1 class")
        body = _plain_dict(value, _MESSAGE_CONTENT_FIELDS, "message content")
        segments = _plain_list(body["segments"], "segments", maximum_items=_MAX_MESSAGE_SEGMENTS)
        attachments = _plain_list(
            body["attachments"], "attachments", maximum_items=_MAX_ATTACHMENTS
        )
        return cls(
            schema_version=body["schemaVersion"],
            segments=tuple(IMMessageSegmentV1.from_dict(item) for item in segments),
            attachments=tuple(IMAttachmentRefV1.from_dict(item) for item in attachments),
        )


@dataclass(frozen=True)
class IMMessageRefV1(_NativeIMWireValue):
    schema_version: int
    conversation: IMConversationRefV1
    message_id: str
    revision: str
    created_at: str

    _MODEL_NAME: ClassVar[str] = "IMMessageRefV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMMessageRefV1, "message reference")
        _schema_version(self.schema_version)
        _require_exact_model(self.conversation, IMConversationRefV1, "conversation")
        _id(self.message_id, "messageId")
        _id(self.revision, "revision")
        _timestamp(self.created_at, "createdAt")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "conversation": self.conversation.to_dict(),
            "messageId": self.message_id,
            "revision": self.revision,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMMessageRefV1:
        if cls is not IMMessageRefV1:
            raise TypeError("message reference decoder requires the exact V1 class")
        body = _plain_dict(value, _MESSAGE_REF_FIELDS, "message reference")
        return cls(
            schema_version=body["schemaVersion"],
            conversation=IMConversationRefV1.from_dict(body["conversation"]),
            message_id=body["messageId"],
            revision=body["revision"],
            created_at=body["createdAt"],
        )


@dataclass(frozen=True)
class IMReactionRefV1(_NativeIMWireValue):
    schema_version: int
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    reaction_key: str

    _MODEL_NAME: ClassVar[str] = "IMReactionRefV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMReactionRefV1, "reaction")
        _schema_version(self.schema_version)
        for value, label in (
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.reaction_key, "reactionKey"),
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
            "reactionKey": self.reaction_key,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMReactionRefV1:
        if cls is not IMReactionRefV1:
            raise TypeError("reaction decoder requires the exact V1 class")
        body = _plain_dict(value, _REACTION_REF_FIELDS, "reaction")
        return cls(
            schema_version=body["schemaVersion"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            reaction_key=body["reactionKey"],
        )


@dataclass(frozen=True)
class IMMembershipChangeV1(_NativeIMWireValue):
    schema_version: int
    subject: IMParticipantRefV1
    change_kind: str
    previous_membership_revision: str | None

    _MODEL_NAME: ClassVar[str] = "IMMembershipChangeV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMMembershipChangeV1, "membership change")
        _schema_version(self.schema_version)
        _require_exact_model(self.subject, IMParticipantRefV1, "subject")
        _enum(self.change_kind, _MEMBERSHIP_CHANGE_KINDS, "changeKind")
        previous = _optional_id(self.previous_membership_revision, "previousMembershipRevision")
        if self.change_kind == "joined":
            if previous is not None:
                raise ValueError("joined membership change must not have a previous revision")
        elif previous is None or previous == self.subject.membership_revision:
            raise ValueError("membership change must bind a distinct previous revision")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "subject": self.subject.to_dict(),
            "changeKind": self.change_kind,
            "previousMembershipRevision": self.previous_membership_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMMembershipChangeV1:
        if cls is not IMMembershipChangeV1:
            raise TypeError("membership change decoder requires the exact V1 class")
        body = _plain_dict(value, _MEMBERSHIP_CHANGE_FIELDS, "membership change")
        return cls(
            schema_version=body["schemaVersion"],
            subject=IMParticipantRefV1.from_dict(body["subject"]),
            change_kind=body["changeKind"],
            previous_membership_revision=body["previousMembershipRevision"],
        )


@dataclass(frozen=True)
class InboundIMEventV1(_NativeIMWireValue):
    schema_version: int
    event_id: str
    event_type: str
    cursor: str
    sequence_number: int
    conversation: IMConversationRefV1
    message: IMMessageRefV1 | None
    sender: IMParticipantRefV1 | None
    content: IMMessageContentV1 | None = field(repr=False)
    reaction: IMReactionRefV1 | None
    membership_change: IMMembershipChangeV1 | None
    occurred_at: str
    first_received_at: str
    ingress_request_id: str
    correlation_id: str
    causation_id: str | None
    transport_evidence_digest: str

    _MODEL_NAME: ClassVar[str] = "InboundIMEventV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, InboundIMEventV1, "inbound event")
        _schema_version(self.schema_version)
        _id(self.event_id, "eventId")
        _enum(self.event_type, _INBOUND_EVENT_TYPES, "eventType")
        _id(self.cursor, "cursor")
        _non_negative_integer(self.sequence_number, "sequenceNumber")
        _require_exact_model(self.conversation, IMConversationRefV1, "conversation")
        if self.message is not None:
            _require_exact_model(self.message, IMMessageRefV1, "message")
        if self.sender is not None:
            _require_exact_model(self.sender, IMParticipantRefV1, "sender")
        if self.content is not None:
            _require_exact_model(self.content, IMMessageContentV1, "content")
        if self.reaction is not None:
            _require_exact_model(self.reaction, IMReactionRefV1, "reaction")
        if self.membership_change is not None:
            _require_exact_model(
                self.membership_change,
                IMMembershipChangeV1,
                "membershipChange",
            )
        self._validate_event_matrix()
        expected_scope = _scope(self.conversation)
        if self.message is not None and self.message.conversation != self.conversation:
            raise ValueError("message conversation does not match inbound event conversation")
        for nested, label in (
            (self.sender, "sender"),
            (self.reaction, "reaction"),
        ):
            if nested is not None and _scope(nested) != expected_scope:
                raise ValueError(f"{label} scope does not match inbound event scope")
        if self.membership_change is not None and (
            _scope(self.membership_change.subject) != expected_scope
        ):
            raise ValueError("membership change scope does not match inbound event scope")
        if self.content is not None:
            for attachment in self.content.attachments:
                if _scope(attachment) != expected_scope:
                    raise ValueError("attachment scope does not match inbound event scope")
        _timestamp(self.occurred_at, "occurredAt")
        _timestamp(self.first_received_at, "firstReceivedAt")
        _id(self.ingress_request_id, "ingressRequestId")
        _id(self.correlation_id, "correlationId")
        _optional_id(self.causation_id, "causationId")
        _digest(self.transport_evidence_digest, "transportEvidenceDigest")
        self.canonical_bytes()

    def _validate_event_matrix(self) -> None:
        values = (self.message, self.sender, self.content, self.reaction, self.membership_change)
        if self.event_type in {"message.created", "message.edited"}:
            valid = all(item is not None for item in values[:3]) and all(
                item is None for item in values[3:]
            )
        elif self.event_type == "message.deleted":
            valid = (
                self.message is not None
                and self.content is None
                and self.reaction is None
                and self.membership_change is None
            )
        elif self.event_type in {"reaction.added", "reaction.removed"}:
            valid = (
                self.message is not None
                and self.sender is not None
                and self.content is None
                and self.reaction is not None
                and self.membership_change is None
            )
        else:
            valid = (
                self.message is None
                and self.content is None
                and self.reaction is None
                and self.membership_change is not None
            )
        if not valid:
            raise ValueError("inbound event fields do not match its eventType matrix")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "eventId": self.event_id,
            "eventType": self.event_type,
            "cursor": self.cursor,
            "sequenceNumber": self.sequence_number,
            "conversation": self.conversation.to_dict(),
            "message": None if self.message is None else self.message.to_dict(),
            "sender": None if self.sender is None else self.sender.to_dict(),
            "content": None if self.content is None else self.content.to_dict(),
            "reaction": None if self.reaction is None else self.reaction.to_dict(),
            "membershipChange": (
                None if self.membership_change is None else self.membership_change.to_dict()
            ),
            "occurredAt": self.occurred_at,
            "firstReceivedAt": self.first_received_at,
            "ingressRequestId": self.ingress_request_id,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "transportEvidenceDigest": self.transport_evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> InboundIMEventV1:
        if cls is not InboundIMEventV1:
            raise TypeError("inbound event decoder requires the exact V1 class")
        body = _plain_dict(value, _INBOUND_EVENT_FIELDS, "inbound event")
        return cls(
            schema_version=body["schemaVersion"],
            event_id=body["eventId"],
            event_type=body["eventType"],
            cursor=body["cursor"],
            sequence_number=body["sequenceNumber"],
            conversation=IMConversationRefV1.from_dict(body["conversation"]),
            message=(
                None if body["message"] is None else IMMessageRefV1.from_dict(body["message"])
            ),
            sender=(
                None if body["sender"] is None else IMParticipantRefV1.from_dict(body["sender"])
            ),
            content=(
                None if body["content"] is None else IMMessageContentV1.from_dict(body["content"])
            ),
            reaction=(
                None if body["reaction"] is None else IMReactionRefV1.from_dict(body["reaction"])
            ),
            membership_change=(
                None
                if body["membershipChange"] is None
                else IMMembershipChangeV1.from_dict(body["membershipChange"])
            ),
            occurred_at=body["occurredAt"],
            first_received_at=body["firstReceivedAt"],
            ingress_request_id=body["ingressRequestId"],
            correlation_id=body["correlationId"],
            causation_id=body["causationId"],
            transport_evidence_digest=body["transportEvidenceDigest"],
        )


@dataclass(frozen=True)
class IMVerifiedInboundEnvelopeV1(_NativeIMWireValue):
    schema_version: int
    event: InboundIMEventV1 = field(repr=False)
    event_digest: str
    verification_id: str
    verifier_id: str
    authentication_evidence_digest: str
    tenant_mapping_revision: str
    verified_at: str
    traceparent: str | None

    _MODEL_NAME: ClassVar[str] = "IMVerifiedInboundEnvelopeV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMVerifiedInboundEnvelopeV1, "verified inbound envelope")
        _schema_version(self.schema_version)
        _require_exact_model(self.event, InboundIMEventV1, "event")
        _digest(self.event_digest, "eventDigest")
        if self.event_digest != self.event.canonical_digest():
            raise ValueError("eventDigest does not match the exact inbound event")
        _id(self.verification_id, "verificationId")
        _id(self.verifier_id, "verifierId")
        _digest(self.authentication_evidence_digest, "authenticationEvidenceDigest")
        _id(self.tenant_mapping_revision, "tenantMappingRevision")
        _timestamp(self.verified_at, "verifiedAt")
        _optional_traceparent(self.traceparent, "traceparent")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "event": self.event.to_dict(),
            "eventDigest": self.event_digest,
            "verificationId": self.verification_id,
            "verifierId": self.verifier_id,
            "authenticationEvidenceDigest": self.authentication_evidence_digest,
            "tenantMappingRevision": self.tenant_mapping_revision,
            "verifiedAt": self.verified_at,
            "traceparent": self.traceparent,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMVerifiedInboundEnvelopeV1:
        if cls is not IMVerifiedInboundEnvelopeV1:
            raise TypeError("verified envelope decoder requires the exact V1 class")
        body = _plain_dict(value, _VERIFIED_ENVELOPE_FIELDS, "verified inbound envelope")
        return cls(
            schema_version=body["schemaVersion"],
            event=InboundIMEventV1.from_dict(body["event"]),
            event_digest=body["eventDigest"],
            verification_id=body["verificationId"],
            verifier_id=body["verifierId"],
            authentication_evidence_digest=body["authenticationEvidenceDigest"],
            tenant_mapping_revision=body["tenantMappingRevision"],
            verified_at=body["verifiedAt"],
            traceparent=body["traceparent"],
        )


@dataclass(frozen=True)
class IMCapabilityRequestV1(_NativeIMWireValue):
    schema_version: int
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    request_id: str

    _MODEL_NAME: ClassVar[str] = "IMCapabilityRequestV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMCapabilityRequestV1, "capability request")
        _schema_version(self.schema_version)
        for value, label in (
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.request_id, "requestId"),
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
            "requestId": self.request_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMCapabilityRequestV1:
        if cls is not IMCapabilityRequestV1:
            raise TypeError("capability request decoder requires the exact V1 class")
        body = _plain_dict(value, _CAPABILITY_REQUEST_FIELDS, "capability request")
        return cls(
            schema_version=body["schemaVersion"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            request_id=body["requestId"],
        )


@dataclass(frozen=True)
class IMAcceptanceLookupCapabilityV1(_NativeIMWireValue):
    schema_version: int
    lookup_mode: str
    negative_acceptance_mode: str
    retention_seconds: int
    consistency_seconds: int

    _MODEL_NAME: ClassVar[str] = "IMAcceptanceLookupCapabilityV1"

    def __post_init__(self) -> None:
        _require_exact_model(
            self,
            IMAcceptanceLookupCapabilityV1,
            "acceptance lookup capability",
        )
        _schema_version(self.schema_version)
        _enum(self.lookup_mode, _LOOKUP_MODES, "lookupMode")
        _enum(
            self.negative_acceptance_mode,
            _NEGATIVE_ACCEPTANCE_MODES,
            "negativeAcceptanceMode",
        )
        _positive_integer(self.retention_seconds, "retentionSeconds")
        _non_negative_integer(self.consistency_seconds, "consistencySeconds")
        if self.consistency_seconds >= self.retention_seconds:
            raise ValueError("consistencySeconds must be less than retentionSeconds")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "lookupMode": self.lookup_mode,
            "negativeAcceptanceMode": self.negative_acceptance_mode,
            "retentionSeconds": self.retention_seconds,
            "consistencySeconds": self.consistency_seconds,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMAcceptanceLookupCapabilityV1:
        if cls is not IMAcceptanceLookupCapabilityV1:
            raise TypeError("acceptance lookup decoder requires the exact V1 class")
        body = _plain_dict(value, _ACCEPTANCE_LOOKUP_FIELDS, "acceptance lookup capability")
        return cls(
            schema_version=body["schemaVersion"],
            lookup_mode=body["lookupMode"],
            negative_acceptance_mode=body["negativeAcceptanceMode"],
            retention_seconds=body["retentionSeconds"],
            consistency_seconds=body["consistencySeconds"],
        )


@dataclass(frozen=True)
class IMOperationCapabilityV1(_NativeIMWireValue):
    schema_version: int
    operation: str
    revision_mode: str
    idempotency_mode: str
    acceptance_lookups: Tuple[IMAcceptanceLookupCapabilityV1, ...]

    _MODEL_NAME: ClassVar[str] = "IMOperationCapabilityV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMOperationCapabilityV1, "operation capability")
        _schema_version(self.schema_version)
        _enum(self.operation, _OPERATIONS, "operation")
        _enum(self.revision_mode, _REVISION_MODES, "revisionMode")
        _enum(self.idempotency_mode, _IDEMPOTENCY_MODES, "idempotencyMode")
        if self.operation in {"send_message", "add_reaction", "remove_reaction"}:
            if self.revision_mode != "not_applicable":
                raise ValueError(
                    "send and reaction operations require not_applicable revision mode"
                )
        elif self.revision_mode == "not_applicable":
            raise ValueError("edit and delete operations require a revision mode")
        _require_exact_tuple(self.acceptance_lookups, "acceptanceLookups")
        if len(self.acceptance_lookups) > len(_LOOKUP_MODES):
            raise NativeIMCodecTooLargeError("acceptanceLookups exceeds its item limit")
        for index, lookup in enumerate(self.acceptance_lookups):
            _require_exact_model(
                lookup,
                IMAcceptanceLookupCapabilityV1,
                f"acceptanceLookups[{index}]",
            )
        lookup_modes = tuple(lookup.lookup_mode for lookup in self.acceptance_lookups)
        _ordered_unique_text(lookup_modes, "acceptanceLookups")
        if "idempotency_key" in lookup_modes and self.idempotency_mode != "receiver_deduplicated":
            raise ValueError("idempotency_key lookup requires receiver_deduplicated idempotency")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "operation": self.operation,
            "revisionMode": self.revision_mode,
            "idempotencyMode": self.idempotency_mode,
            "acceptanceLookups": [lookup.to_dict() for lookup in self.acceptance_lookups],
        }

    @classmethod
    def from_dict(cls, value: object) -> IMOperationCapabilityV1:
        if cls is not IMOperationCapabilityV1:
            raise TypeError("operation capability decoder requires the exact V1 class")
        body = _plain_dict(value, _OPERATION_CAPABILITY_FIELDS, "operation capability")
        lookups = _plain_list(
            body["acceptanceLookups"],
            "acceptanceLookups",
            maximum_items=len(_LOOKUP_MODES),
        )
        return cls(
            schema_version=body["schemaVersion"],
            operation=body["operation"],
            revision_mode=body["revisionMode"],
            idempotency_mode=body["idempotencyMode"],
            acceptance_lookups=tuple(
                IMAcceptanceLookupCapabilityV1.from_dict(item) for item in lookups
            ),
        )


@dataclass(frozen=True)
class IMCapabilitySnapshotV1(_NativeIMWireValue):
    schema_version: int
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    revision: str
    observed_at: str
    operations: Tuple[IMOperationCapabilityV1, ...]
    idempotency_retention_seconds: int | None
    supports_threads: bool
    supports_mentions: bool
    supports_attachments: bool
    supports_membership_events: bool
    max_text_bytes: int
    max_attachments: int
    max_attachment_bytes: int

    _MODEL_NAME: ClassVar[str] = "IMCapabilitySnapshotV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMCapabilitySnapshotV1, "capability snapshot")
        _schema_version(self.schema_version)
        for value, label in (
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.revision, "revision"),
        ):
            _id(value, label)
        _timestamp(self.observed_at, "observedAt")
        _require_exact_tuple(self.operations, "operations")
        if len(self.operations) > len(_OPERATIONS):
            raise NativeIMCodecTooLargeError("operations exceeds its item limit")
        for index, operation in enumerate(self.operations):
            _require_exact_model(
                operation,
                IMOperationCapabilityV1,
                f"operations[{index}]",
            )
        operation_names = tuple(operation.operation for operation in self.operations)
        _ordered_unique_text(operation_names, "operations")
        has_receiver_idempotency = any(
            operation.idempotency_mode == "receiver_deduplicated" for operation in self.operations
        )
        if has_receiver_idempotency:
            if self.idempotency_retention_seconds is None:
                raise ValueError("receiver-deduplicated capability requires retention")
            _positive_integer(
                self.idempotency_retention_seconds,
                "idempotencyRetentionSeconds",
            )
        elif self.idempotency_retention_seconds is not None:
            raise ValueError("idempotency retention requires a receiver-deduplicated operation")
        for boolean, label in (
            (self.supports_threads, "supportsThreads"),
            (self.supports_mentions, "supportsMentions"),
            (self.supports_attachments, "supportsAttachments"),
            (self.supports_membership_events, "supportsMembershipEvents"),
        ):
            _boolean(boolean, label)
        _non_negative_integer(self.max_text_bytes, "maxTextBytes")
        _non_negative_integer(self.max_attachments, "maxAttachments")
        _non_negative_integer(self.max_attachment_bytes, "maxAttachmentBytes")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "provider": self.provider,
            "channelId": self.channel_id,
            "revision": self.revision,
            "observedAt": self.observed_at,
            "operations": [operation.to_dict() for operation in self.operations],
            "idempotencyRetentionSeconds": self.idempotency_retention_seconds,
            "supportsThreads": self.supports_threads,
            "supportsMentions": self.supports_mentions,
            "supportsAttachments": self.supports_attachments,
            "supportsMembershipEvents": self.supports_membership_events,
            "maxTextBytes": self.max_text_bytes,
            "maxAttachments": self.max_attachments,
            "maxAttachmentBytes": self.max_attachment_bytes,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMCapabilitySnapshotV1:
        if cls is not IMCapabilitySnapshotV1:
            raise TypeError("capability snapshot decoder requires the exact V1 class")
        body = _plain_dict(value, _CAPABILITY_SNAPSHOT_FIELDS, "capability snapshot")
        operations = _plain_list(body["operations"], "operations", maximum_items=len(_OPERATIONS))
        return cls(
            schema_version=body["schemaVersion"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            revision=body["revision"],
            observed_at=body["observedAt"],
            operations=tuple(IMOperationCapabilityV1.from_dict(item) for item in operations),
            idempotency_retention_seconds=body["idempotencyRetentionSeconds"],
            supports_threads=body["supportsThreads"],
            supports_mentions=body["supportsMentions"],
            supports_attachments=body["supportsAttachments"],
            supports_membership_events=body["supportsMembershipEvents"],
            max_text_bytes=body["maxTextBytes"],
            max_attachments=body["maxAttachments"],
            max_attachment_bytes=body["maxAttachmentBytes"],
        )


@dataclass(frozen=True)
class IMInboundReadRequestV1(_NativeIMWireValue):
    schema_version: int
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    after_cursor: str | None
    after_sequence: int | None
    snapshot_token: str | None
    limit: int
    read_request_id: str

    _MODEL_NAME: ClassVar[str] = "IMInboundReadRequestV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMInboundReadRequestV1, "inbound read request")
        _schema_version(self.schema_version)
        for value, label in (
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.read_request_id, "readRequestId"),
        ):
            _id(value, label)
        _optional_id(self.after_cursor, "afterCursor")
        if self.after_sequence is not None:
            _non_negative_integer(self.after_sequence, "afterSequence")
        if (self.after_cursor is None) != (self.after_sequence is None):
            raise ValueError("afterCursor and afterSequence must be null or present together")
        _optional_id(self.snapshot_token, "snapshotToken")
        if self.snapshot_token is not None and self.after_cursor is None:
            raise ValueError("snapshotToken continuation requires an after pair")
        _positive_integer(self.limit, "limit")
        if self.limit > _MAX_INBOUND_ENVELOPES:
            raise ValueError("limit exceeds the V1 maximum")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "provider": self.provider,
            "channelId": self.channel_id,
            "afterCursor": self.after_cursor,
            "afterSequence": self.after_sequence,
            "snapshotToken": self.snapshot_token,
            "limit": self.limit,
            "readRequestId": self.read_request_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMInboundReadRequestV1:
        if cls is not IMInboundReadRequestV1:
            raise TypeError("inbound read request decoder requires the exact V1 class")
        body = _plain_dict(value, _INBOUND_READ_REQUEST_FIELDS, "inbound read request")
        return cls(
            schema_version=body["schemaVersion"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            after_cursor=body["afterCursor"],
            after_sequence=body["afterSequence"],
            snapshot_token=body["snapshotToken"],
            limit=body["limit"],
            read_request_id=body["readRequestId"],
        )


@dataclass(frozen=True)
class IMInboundPageV1(_NativeIMWireValue):
    schema_version: int
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    read_request_id: str
    read_request_digest: str
    snapshot_token: str
    envelopes: Tuple[IMVerifiedInboundEnvelopeV1, ...] = field(repr=False)
    next_cursor: str | None
    next_sequence: int | None
    has_more: bool
    capability_revision: str
    capability_digest: str

    _MODEL_NAME: ClassVar[str] = "IMInboundPageV1"
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_INBOUND_PAGE_BYTES

    def __post_init__(self) -> None:
        _require_exact_model(self, IMInboundPageV1, "inbound page")
        _schema_version(self.schema_version)
        for value, label in (
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.read_request_id, "readRequestId"),
            (self.snapshot_token, "snapshotToken"),
            (self.capability_revision, "capabilityRevision"),
        ):
            _id(value, label)
        _digest(self.read_request_digest, "readRequestDigest")
        _digest(self.capability_digest, "capabilityDigest")
        _require_exact_tuple(self.envelopes, "envelopes")
        if len(self.envelopes) > _MAX_INBOUND_ENVELOPES:
            raise NativeIMCodecTooLargeError("envelopes exceeds its item limit")
        expected_scope = _scope(self)
        previous_sequence: int | None = None
        event_ids: set[str] = set()
        envelope_canonical_bytes = 0
        for index, envelope in enumerate(self.envelopes):
            _require_exact_model(
                envelope,
                IMVerifiedInboundEnvelopeV1,
                f"envelopes[{index}]",
            )
            event = envelope.event
            if _scope(event.conversation) != expected_scope:
                raise ValueError("envelope scope does not match inbound page scope")
            if previous_sequence is not None and event.sequence_number <= previous_sequence:
                raise ValueError("inbound page sequenceNumber values must strictly increase")
            if event.event_id in event_ids:
                raise ValueError("inbound page eventId values must be unique")
            event_ids.add(event.event_id)
            previous_sequence = event.sequence_number
            envelope_canonical_bytes += len(envelope.canonical_bytes())
            if envelope_canonical_bytes > self._MAX_CANONICAL_BYTES:
                raise NativeIMCodecTooLargeError(
                    "inbound page envelopes exceed its canonical byte limit"
                )
        _optional_id(self.next_cursor, "nextCursor")
        if self.next_sequence is not None:
            _non_negative_integer(self.next_sequence, "nextSequence")
        if (self.next_cursor is None) != (self.next_sequence is None):
            raise ValueError("nextCursor and nextSequence must be null or present together")
        _boolean(self.has_more, "hasMore")
        if self.envelopes:
            final_event = self.envelopes[-1].event
            if (self.next_cursor, self.next_sequence) != (
                final_event.cursor,
                final_event.sequence_number,
            ):
                raise ValueError("next pair must equal the final inbound event")
        elif self.has_more:
            raise ValueError("hasMore requires a non-empty inbound page")
        self.canonical_bytes()

    def validate_request_binding(self, request: IMInboundReadRequestV1) -> None:
        _require_exact_model(request, IMInboundReadRequestV1, "inbound read request")
        if _scope(self) != _scope(request):
            raise ValueError("inbound page scope does not match its read request")
        if self.read_request_id != request.read_request_id:
            raise ValueError("readRequestId does not match its read request")
        if self.read_request_digest != request.canonical_digest():
            raise ValueError("readRequestDigest does not match its read request")
        if request.snapshot_token is not None and self.snapshot_token != request.snapshot_token:
            raise ValueError("snapshotToken does not continue the requested snapshot")
        if len(self.envelopes) > request.limit:
            raise ValueError("inbound page exceeds the requested limit")
        if self.envelopes:
            if (
                request.after_sequence is not None
                and self.envelopes[0].event.sequence_number <= request.after_sequence
            ):
                raise ValueError("first inbound sequence does not advance the request")
            if self.has_more and (self.next_cursor, self.next_sequence) == (
                request.after_cursor,
                request.after_sequence,
            ):
                raise ValueError("hasMore page does not advance its resume pair")
        elif (self.next_cursor, self.next_sequence) != (
            request.after_cursor,
            request.after_sequence,
        ):
            raise ValueError("empty page must preserve the request resume pair")

    def validate_capability_binding(self, capability: IMCapabilitySnapshotV1) -> None:
        _require_exact_model(capability, IMCapabilitySnapshotV1, "capability snapshot")
        if _scope(self) != _scope(capability):
            raise ValueError("inbound page scope does not match its capability snapshot")
        if self.capability_revision != capability.revision:
            raise ValueError("capabilityRevision does not match its snapshot")
        if self.capability_digest != capability.canonical_digest():
            raise ValueError("capabilityDigest does not match its snapshot")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "provider": self.provider,
            "channelId": self.channel_id,
            "readRequestId": self.read_request_id,
            "readRequestDigest": self.read_request_digest,
            "snapshotToken": self.snapshot_token,
            "envelopes": [envelope.to_dict() for envelope in self.envelopes],
            "nextCursor": self.next_cursor,
            "nextSequence": self.next_sequence,
            "hasMore": self.has_more,
            "capabilityRevision": self.capability_revision,
            "capabilityDigest": self.capability_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMInboundPageV1:
        if cls is not IMInboundPageV1:
            raise TypeError("inbound page decoder requires the exact V1 class")
        body = _plain_dict(value, _INBOUND_PAGE_FIELDS, "inbound page")
        envelopes = _plain_list(
            body["envelopes"],
            "envelopes",
            maximum_items=_MAX_INBOUND_ENVELOPES,
        )
        return cls(
            schema_version=body["schemaVersion"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            read_request_id=body["readRequestId"],
            read_request_digest=body["readRequestDigest"],
            snapshot_token=body["snapshotToken"],
            envelopes=tuple(IMVerifiedInboundEnvelopeV1.from_dict(item) for item in envelopes),
            next_cursor=body["nextCursor"],
            next_sequence=body["nextSequence"],
            has_more=body["hasMore"],
            capability_revision=body["capabilityRevision"],
            capability_digest=body["capabilityDigest"],
        )


@dataclass(frozen=True)
class IMActionIntentV1(_NativeIMWireValue):
    schema_version: int
    action_id: str
    tenant_id: str
    workspace_id: str
    actor_id: str
    delegator_id: str | None
    conversation: IMConversationRefV1
    operation: str
    target_message: IMMessageRefV1 | None
    content: IMMessageContentV1 | None = field(repr=False)
    reaction: IMReactionRefV1 | None
    created_at: str
    correlation_id: str
    causation_id: str
    traceparent: str | None

    _MODEL_NAME: ClassVar[str] = "IMActionIntentV1"
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_ACTION_BYTES

    def __post_init__(self) -> None:
        _require_exact_model(self, IMActionIntentV1, "action intent")
        _schema_version(self.schema_version)
        for value, label in (
            (self.action_id, "actionId"),
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.actor_id, "actorId"),
            (self.correlation_id, "correlationId"),
            (self.causation_id, "causationId"),
        ):
            _id(value, label)
        _optional_id(self.delegator_id, "delegatorId")
        _require_exact_model(self.conversation, IMConversationRefV1, "conversation")
        _enum(self.operation, _OPERATIONS, "operation")
        if self.target_message is not None:
            _require_exact_model(self.target_message, IMMessageRefV1, "targetMessage")
        if self.content is not None:
            _require_exact_model(self.content, IMMessageContentV1, "content")
        if self.reaction is not None:
            _require_exact_model(self.reaction, IMReactionRefV1, "reaction")
        self._validate_operation_matrix()
        if (self.tenant_id, self.workspace_id) != (
            self.conversation.tenant_id,
            self.conversation.workspace_id,
        ):
            raise ValueError("action intent tenant/workspace does not match its conversation")
        if (
            self.target_message is not None
            and self.target_message.conversation != self.conversation
        ):
            raise ValueError("target message conversation does not match the action intent")
        expected_scope = _scope(self.conversation)
        if self.reaction is not None and _scope(self.reaction) != expected_scope:
            raise ValueError("reaction scope does not match the action intent")
        if self.content is not None:
            for attachment in self.content.attachments:
                if _scope(attachment) != expected_scope:
                    raise ValueError("attachment scope does not match the action intent")
        _timestamp(self.created_at, "createdAt")
        _optional_traceparent(self.traceparent, "traceparent")
        self.canonical_bytes()

    def _validate_operation_matrix(self) -> None:
        if self.operation == "send_message":
            valid = (
                self.target_message is None and self.content is not None and self.reaction is None
            )
        elif self.operation == "edit_message":
            valid = (
                self.target_message is not None
                and self.content is not None
                and self.reaction is None
            )
        elif self.operation == "delete_message":
            valid = (
                self.target_message is not None and self.content is None and self.reaction is None
            )
        elif self.operation in {"add_reaction", "remove_reaction"}:
            valid = (
                self.target_message is not None
                and self.content is None
                and self.reaction is not None
            )
        else:  # pragma: no cover - the enum validator fails first; retain fail-closed locality.
            valid = False
        if not valid:
            raise ValueError("action intent fields do not match its operation matrix")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "actionId": self.action_id,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "actorId": self.actor_id,
            "delegatorId": self.delegator_id,
            "conversation": self.conversation.to_dict(),
            "operation": self.operation,
            "targetMessage": (
                None if self.target_message is None else self.target_message.to_dict()
            ),
            "content": None if self.content is None else self.content.to_dict(),
            "reaction": None if self.reaction is None else self.reaction.to_dict(),
            "createdAt": self.created_at,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "traceparent": self.traceparent,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMActionIntentV1:
        if cls is not IMActionIntentV1:
            raise TypeError("action intent decoder requires the exact V1 class")
        body = _plain_dict(value, _ACTION_INTENT_FIELDS, "action intent")
        return cls(
            schema_version=body["schemaVersion"],
            action_id=body["actionId"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            actor_id=body["actorId"],
            delegator_id=body["delegatorId"],
            conversation=IMConversationRefV1.from_dict(body["conversation"]),
            operation=body["operation"],
            target_message=(
                None
                if body["targetMessage"] is None
                else IMMessageRefV1.from_dict(body["targetMessage"])
            ),
            content=(
                None if body["content"] is None else IMMessageContentV1.from_dict(body["content"])
            ),
            reaction=(
                None if body["reaction"] is None else IMReactionRefV1.from_dict(body["reaction"])
            ),
            created_at=body["createdAt"],
            correlation_id=body["correlationId"],
            causation_id=body["causationId"],
            traceparent=body["traceparent"],
        )


def derive_im_idempotency_key_v1(intent: IMActionIntentV1) -> str:
    """Derive the frozen receiver key from one exact, already validated action intent."""

    _require_exact_model(intent, IMActionIntentV1, "action intent")
    body = {
        "actionId": intent.action_id,
        "channelId": intent.conversation.channel_id,
        "provider": intent.conversation.provider,
        "tenantId": intent.tenant_id,
        "workspaceId": intent.workspace_id,
    }
    domain = b"quantum-entanglement.native-im/idempotency-key/1\n"
    return hashlib.sha256(domain + _canonical_json_bytes(body)).hexdigest()


@dataclass(frozen=True)
class IMActionCommandV1(_NativeIMWireValue):
    schema_version: int
    command_id: str
    intent: IMActionIntentV1 = field(repr=False)
    intent_digest: str
    idempotency_key: str
    authorization_decision_id: str
    authorization_revision: str
    approval_decision_id: str | None
    approval_revision: str | None
    policy_revision: str
    capability_revision: str
    capability_digest: str
    authorized_at: str
    expires_at: str
    correlation_id: str
    causation_id: str
    traceparent: str | None

    _MODEL_NAME: ClassVar[str] = "IMActionCommandV1"
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_ACTION_BYTES

    def __post_init__(self) -> None:
        _require_exact_model(self, IMActionCommandV1, "action command")
        _schema_version(self.schema_version)
        _id(self.command_id, "commandId")
        _require_exact_model(self.intent, IMActionIntentV1, "intent")
        _digest(self.intent_digest, "intentDigest")
        if self.intent_digest != self.intent.canonical_digest():
            raise ValueError("intentDigest does not match the exact action intent")
        _digest(self.idempotency_key, "idempotencyKey")
        if self.idempotency_key != derive_im_idempotency_key_v1(self.intent):
            raise ValueError("idempotencyKey does not match the exact action scope")
        for value, label in (
            (self.authorization_decision_id, "authorizationDecisionId"),
            (self.authorization_revision, "authorizationRevision"),
            (self.policy_revision, "policyRevision"),
            (self.capability_revision, "capabilityRevision"),
            (self.correlation_id, "correlationId"),
            (self.causation_id, "causationId"),
        ):
            _id(value, label)
        _optional_id(self.approval_decision_id, "approvalDecisionId")
        _optional_id(self.approval_revision, "approvalRevision")
        if (self.approval_decision_id is None) != (self.approval_revision is None):
            raise ValueError("approval decision ID and revision must be null or present together")
        _digest(self.capability_digest, "capabilityDigest")
        _timestamp(self.authorized_at, "authorizedAt")
        _timestamp(self.expires_at, "expiresAt")
        if self.authorized_at >= self.expires_at:
            raise ValueError("authorizedAt must be earlier than expiresAt")
        _optional_traceparent(self.traceparent, "traceparent")
        if self.correlation_id != self.intent.correlation_id:
            raise ValueError("correlationId does not match the action intent")
        if self.causation_id != self.intent.action_id:
            raise ValueError("causationId must equal the action ID")
        if self.traceparent != self.intent.traceparent:
            raise ValueError("traceparent does not match the action intent")
        self.canonical_bytes()

    def validate_capability_binding(self, capability: IMCapabilitySnapshotV1) -> None:
        """Validate a snapshot binding after the caller has read it from a trusted store."""

        _require_exact_model(capability, IMCapabilitySnapshotV1, "capability snapshot")
        if _scope(self.intent.conversation) != _scope(capability):
            raise ValueError("action command scope does not match its capability snapshot")
        if self.capability_revision != capability.revision:
            raise ValueError("capabilityRevision does not match its snapshot")
        if self.capability_digest != capability.canonical_digest():
            raise ValueError("capabilityDigest does not match its snapshot")
        if not any(profile.operation == self.intent.operation for profile in capability.operations):
            raise ValueError("capability snapshot does not enable the action operation")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "commandId": self.command_id,
            "intent": self.intent.to_dict(),
            "intentDigest": self.intent_digest,
            "idempotencyKey": self.idempotency_key,
            "authorizationDecisionId": self.authorization_decision_id,
            "authorizationRevision": self.authorization_revision,
            "approvalDecisionId": self.approval_decision_id,
            "approvalRevision": self.approval_revision,
            "policyRevision": self.policy_revision,
            "capabilityRevision": self.capability_revision,
            "capabilityDigest": self.capability_digest,
            "authorizedAt": self.authorized_at,
            "expiresAt": self.expires_at,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "traceparent": self.traceparent,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMActionCommandV1:
        if cls is not IMActionCommandV1:
            raise TypeError("action command decoder requires the exact V1 class")
        body = _plain_dict(value, _ACTION_COMMAND_FIELDS, "action command")
        return cls(
            schema_version=body["schemaVersion"],
            command_id=body["commandId"],
            intent=IMActionIntentV1.from_dict(body["intent"]),
            intent_digest=body["intentDigest"],
            idempotency_key=body["idempotencyKey"],
            authorization_decision_id=body["authorizationDecisionId"],
            authorization_revision=body["authorizationRevision"],
            approval_decision_id=body["approvalDecisionId"],
            approval_revision=body["approvalRevision"],
            policy_revision=body["policyRevision"],
            capability_revision=body["capabilityRevision"],
            capability_digest=body["capabilityDigest"],
            authorized_at=body["authorizedAt"],
            expires_at=body["expiresAt"],
            correlation_id=body["correlationId"],
            causation_id=body["causationId"],
            traceparent=body["traceparent"],
        )


@dataclass(frozen=True)
class IMDispatchRequestV1(_NativeIMWireValue):
    schema_version: int
    dispatch_attempt_id: str
    command: IMActionCommandV1 = field(repr=False)
    command_digest: str
    attempt_number: int
    fence_id: str
    fence_revision: str
    claimed_at: str
    dispatch_deadline_at: str
    correlation_id: str
    causation_id: str
    traceparent: str | None

    _MODEL_NAME: ClassVar[str] = "IMDispatchRequestV1"
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_ACTION_BYTES

    def __post_init__(self) -> None:
        _require_exact_model(self, IMDispatchRequestV1, "dispatch request")
        _schema_version(self.schema_version)
        _id(self.dispatch_attempt_id, "dispatchAttemptId")
        _require_exact_model(self.command, IMActionCommandV1, "command")
        _digest(self.command_digest, "commandDigest")
        if self.command_digest != self.command.canonical_digest():
            raise ValueError("commandDigest does not match the exact action command")
        _positive_integer(self.attempt_number, "attemptNumber")
        _id(self.fence_id, "fenceId")
        _id(self.fence_revision, "fenceRevision")
        _timestamp(self.claimed_at, "claimedAt")
        _timestamp(self.dispatch_deadline_at, "dispatchDeadlineAt")
        if self.claimed_at >= self.dispatch_deadline_at:
            raise ValueError("claimedAt must be earlier than dispatchDeadlineAt")
        if self.dispatch_deadline_at > self.command.expires_at:
            raise ValueError("dispatchDeadlineAt must not exceed command expiresAt")
        _id(self.correlation_id, "correlationId")
        _id(self.causation_id, "causationId")
        _optional_traceparent(self.traceparent, "traceparent")
        if self.correlation_id != self.command.correlation_id:
            raise ValueError("correlationId does not match the action command")
        if self.causation_id != self.command.command_id:
            raise ValueError("causationId must equal the command ID")
        if self.traceparent != self.command.traceparent:
            raise ValueError("traceparent does not match the action command")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "dispatchAttemptId": self.dispatch_attempt_id,
            "command": self.command.to_dict(),
            "commandDigest": self.command_digest,
            "attemptNumber": self.attempt_number,
            "fenceId": self.fence_id,
            "fenceRevision": self.fence_revision,
            "claimedAt": self.claimed_at,
            "dispatchDeadlineAt": self.dispatch_deadline_at,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "traceparent": self.traceparent,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMDispatchRequestV1:
        if cls is not IMDispatchRequestV1:
            raise TypeError("dispatch request decoder requires the exact V1 class")
        body = _plain_dict(value, _DISPATCH_REQUEST_FIELDS, "dispatch request")
        return cls(
            schema_version=body["schemaVersion"],
            dispatch_attempt_id=body["dispatchAttemptId"],
            command=IMActionCommandV1.from_dict(body["command"]),
            command_digest=body["commandDigest"],
            attempt_number=body["attemptNumber"],
            fence_id=body["fenceId"],
            fence_revision=body["fenceRevision"],
            claimed_at=body["claimedAt"],
            dispatch_deadline_at=body["dispatchDeadlineAt"],
            correlation_id=body["correlationId"],
            causation_id=body["causationId"],
            traceparent=body["traceparent"],
        )


@dataclass(frozen=True)
class IMActionReceiptV1(_NativeIMWireValue):
    schema_version: int
    receipt_id: str
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    action_id: str
    command_id: str
    dispatch_attempt_id: str
    dispatch_request_digest: str
    intent_digest: str
    command_digest: str
    idempotency_key: str
    attempt_number: int
    state: str
    provider_operation_id: str | None
    provider_message: IMMessageRefV1 | None
    receiver_evidence_digest: str | None = field(repr=False)
    error_code: str | None
    retry_after_seconds: int | None
    observed_at: str
    correlation_id: str
    causation_id: str
    traceparent: str | None

    _MODEL_NAME: ClassVar[str] = "IMActionReceiptV1"
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_RECEIPT_BYTES

    def __post_init__(self) -> None:
        _require_exact_model(self, IMActionReceiptV1, "action receipt")
        _schema_version(self.schema_version)
        for value, label in (
            (self.receipt_id, "receiptId"),
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.action_id, "actionId"),
            (self.command_id, "commandId"),
            (self.dispatch_attempt_id, "dispatchAttemptId"),
            (self.correlation_id, "correlationId"),
            (self.causation_id, "causationId"),
        ):
            _id(value, label)
        _digest(self.dispatch_request_digest, "dispatchRequestDigest")
        _digest(self.intent_digest, "intentDigest")
        _digest(self.command_digest, "commandDigest")
        _digest(self.idempotency_key, "idempotencyKey")
        _positive_integer(self.attempt_number, "attemptNumber")
        _enum(self.state, _RECEIPT_STATES, "state")
        _optional_id(self.provider_operation_id, "providerOperationId")
        if self.provider_message is not None:
            _require_exact_model(self.provider_message, IMMessageRefV1, "providerMessage")
            if _scope(self.provider_message.conversation) != _scope(self):
                raise ValueError("provider message scope does not match the action receipt")
        if self.receiver_evidence_digest is not None:
            _digest(self.receiver_evidence_digest, "receiverEvidenceDigest")
        if self.error_code is not None:
            _enum(self.error_code, _RECEIPT_ERROR_CODES, "errorCode")
        if self.retry_after_seconds is not None:
            _positive_integer(self.retry_after_seconds, "retryAfterSeconds")
        self._validate_state_matrix()
        _timestamp(self.observed_at, "observedAt")
        _optional_traceparent(self.traceparent, "traceparent")
        self.canonical_bytes()

    def _validate_state_matrix(self) -> None:
        if self.state in {"succeeded", "reconciled_succeeded"}:
            valid = (
                self.receiver_evidence_digest is not None
                and (self.provider_operation_id is not None or self.provider_message is not None)
                and self.error_code is None
                and self.retry_after_seconds is None
            )
        elif self.state == "rejected":
            valid = (
                self.receiver_evidence_digest is not None
                and self.provider_message is None
                and self.error_code in _TERMINAL_ERROR_CODES
                and self.retry_after_seconds is None
            )
        elif self.state == "retryable_not_accepted":
            valid = (
                self.receiver_evidence_digest is not None
                and self.provider_operation_id is None
                and self.provider_message is None
                and self.error_code in _TRANSIENT_NOT_ACCEPTED_ERROR_CODES
                and (
                    self.error_code != "rate_limited_not_accepted"
                    or self.retry_after_seconds is not None
                )
            )
        elif self.state == "effect_unknown":
            valid = (
                self.provider_message is None
                and self.error_code in _UNKNOWN_ERROR_CODES
                and self.retry_after_seconds is None
            )
        elif self.state == "reconciled_rejected":
            valid = (
                self.receiver_evidence_digest is not None
                and self.provider_message is None
                and self.error_code in _TERMINAL_ERROR_CODES
                and self.retry_after_seconds is None
            )
        else:  # pragma: no cover - the state enum validator fails first.
            valid = False
        if not valid:
            raise ValueError("action receipt fields do not match its state matrix")

    def validate_dispatch_binding(self, request: IMDispatchRequestV1) -> None:
        """Bind a dispatch receipt to the exact durable request returned by store lookup."""

        _require_exact_model(request, IMDispatchRequestV1, "dispatch request")
        if self.state not in _DISPATCH_RECEIPT_STATES:
            raise ValueError("dispatch cannot return a reconciled receipt state")
        command = request.command
        intent = command.intent
        expected_scope = _scope(intent.conversation)
        if _scope(self) != expected_scope:
            raise ValueError("action receipt scope does not match its dispatch request")
        bindings = (
            (self.action_id, intent.action_id, "actionId"),
            (self.command_id, command.command_id, "commandId"),
            (self.dispatch_attempt_id, request.dispatch_attempt_id, "dispatchAttemptId"),
            (
                self.dispatch_request_digest,
                request.canonical_digest(),
                "dispatchRequestDigest",
            ),
            (self.intent_digest, command.intent_digest, "intentDigest"),
            (self.command_digest, request.command_digest, "commandDigest"),
            (self.idempotency_key, command.idempotency_key, "idempotencyKey"),
            (self.attempt_number, request.attempt_number, "attemptNumber"),
            (self.correlation_id, command.correlation_id, "correlationId"),
            (self.traceparent, command.traceparent, "traceparent"),
        )
        for actual, expected, label in bindings:
            if actual != expected:
                raise ValueError(f"{label} does not match the dispatch request")
        if self.causation_id != request.dispatch_attempt_id:
            raise ValueError("dispatch receipt causationId must equal the dispatch attempt ID")
        if (
            self.provider_message is not None
            and self.provider_message.conversation != intent.conversation
        ):
            raise ValueError("provider message conversation does not match the action intent")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "receiptId": self.receipt_id,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "provider": self.provider,
            "channelId": self.channel_id,
            "actionId": self.action_id,
            "commandId": self.command_id,
            "dispatchAttemptId": self.dispatch_attempt_id,
            "dispatchRequestDigest": self.dispatch_request_digest,
            "intentDigest": self.intent_digest,
            "commandDigest": self.command_digest,
            "idempotencyKey": self.idempotency_key,
            "attemptNumber": self.attempt_number,
            "state": self.state,
            "providerOperationId": self.provider_operation_id,
            "providerMessage": (
                None if self.provider_message is None else self.provider_message.to_dict()
            ),
            "receiverEvidenceDigest": self.receiver_evidence_digest,
            "errorCode": self.error_code,
            "retryAfterSeconds": self.retry_after_seconds,
            "observedAt": self.observed_at,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "traceparent": self.traceparent,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMActionReceiptV1:
        if cls is not IMActionReceiptV1:
            raise TypeError("action receipt decoder requires the exact V1 class")
        body = _plain_dict(value, _ACTION_RECEIPT_FIELDS, "action receipt")
        return cls(
            schema_version=body["schemaVersion"],
            receipt_id=body["receiptId"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            action_id=body["actionId"],
            command_id=body["commandId"],
            dispatch_attempt_id=body["dispatchAttemptId"],
            dispatch_request_digest=body["dispatchRequestDigest"],
            intent_digest=body["intentDigest"],
            command_digest=body["commandDigest"],
            idempotency_key=body["idempotencyKey"],
            attempt_number=body["attemptNumber"],
            state=body["state"],
            provider_operation_id=body["providerOperationId"],
            provider_message=(
                None
                if body["providerMessage"] is None
                else IMMessageRefV1.from_dict(body["providerMessage"])
            ),
            receiver_evidence_digest=body["receiverEvidenceDigest"],
            error_code=body["errorCode"],
            retry_after_seconds=body["retryAfterSeconds"],
            observed_at=body["observedAt"],
            correlation_id=body["correlationId"],
            causation_id=body["causationId"],
            traceparent=body["traceparent"],
        )


__all__ = [
    "IMAcceptanceLookupCapabilityV1",
    "IMActionCommandV1",
    "IMActionIntentV1",
    "IMActionReceiptV1",
    "IMCapabilityRequestV1",
    "IMAttachmentRefV1",
    "IMCapabilitySnapshotV1",
    "IMConversationRefV1",
    "IMDispatchRequestV1",
    "IMInboundPageV1",
    "IMInboundReadRequestV1",
    "IMMembershipChangeV1",
    "IMMessageContentV1",
    "IMMessageRefV1",
    "IMMessageSegmentV1",
    "IMOperationCapabilityV1",
    "IMParticipantRefV1",
    "IMReactionRefV1",
    "IMVerifiedInboundEnvelopeV1",
    "InboundIMEventV1",
    "NATIVE_IM_SCHEMA_VERSION",
    "derive_im_idempotency_key_v1",
]
