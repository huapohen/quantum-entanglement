# ruff: noqa: UP006, UP035
"""Executable provider-neutral value contract for native IM V1.

This module contains immutable wire values only.  A codec-valid value is not authenticated,
durable, authorized, or permitted to perform an external effect.  Provider adapters and
composition roots must establish those facts at their dedicated boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Tuple, Type, TypeVar, cast

from ._native_im_codec import (
    NATIVE_IM_SCHEMA_VERSION,
    NativeIMCodecTooLargeError,
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
    _schema_version,
    _timestamp,
)

_MAX_REFERENCE_BYTES = 3 * 1_024 * 1_024
_MAX_MESSAGE_CONTENT_BYTES = 2 * 1_024 * 1_024
_MAX_ROLE_IDS = 1_024
_MAX_MESSAGE_SEGMENTS = 4_096
_MAX_ATTACHMENTS = 64

_PARTICIPANT_KINDS = {"human", "agent", "service"}
_SEGMENT_KINDS = {"text", "mention"}
_MEMBERSHIP_CHANGE_KINDS = {"joined", "left", "role_changed", "suspended", "restored"}

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


def _require_exact_model(value: object, expected: type, label: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{label} must use the exact V1 model class")


def _require_exact_tuple(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    return value


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


__all__ = [
    "IMAttachmentRefV1",
    "IMConversationRefV1",
    "IMMembershipChangeV1",
    "IMMessageContentV1",
    "IMMessageRefV1",
    "IMMessageSegmentV1",
    "IMParticipantRefV1",
    "IMReactionRefV1",
    "NATIVE_IM_SCHEMA_VERSION",
]
