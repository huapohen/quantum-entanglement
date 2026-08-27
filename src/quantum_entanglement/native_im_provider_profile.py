# ruff: noqa: UP006, UP035
"""Version-pinned, provider-specific facts for native-IM E2.

The frozen :mod:`quantum_entanglement.native_im` values remain provider neutral.  This
module records the facts needed to map one provider into that contract without adding an
endpoint, a credential, a transport, or any external effect.  A schema-valid profile is
not by itself permission to connect to a sandbox; E2 readiness is evaluated separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Tuple, Type, TypeVar, cast

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
    _utf8_text,
)
from .native_im import (
    IMCapabilityRequestV1,
    IMCapabilitySnapshotV1,
    IMOperationCapabilityV1,
)

_MAX_PROFILE_BYTES = 256 * 1_024
_MAX_PROFILE_COMPONENT_BYTES = 64 * 1_024
_MAX_ALLOWED_CONVERSATIONS = 128
_MAX_REPLAY_WINDOW_SECONDS = 86_400

_STATUSES = {"supported", "unsupported", "unverified"}
_ENVIRONMENT_CLASSES = {"sandbox", "production"}
_CANONICAL_FIELDS = {
    "attachmentId",
    "attachmentVersion",
    "channelId",
    "conversationId",
    "cursor",
    "eventId",
    "membershipRevision",
    "messageId",
    "messageRevision",
    "participantId",
    "providerMessageId",
    "providerOperationId",
    "reactionKey",
    "sequenceNumber",
    "snapshotToken",
    "tenantId",
    "threadId",
    "workspaceId",
}
_MAPPING_MODES = {
    "canonical_utc_microseconds",
    "configured_constant",
    "exact_enum_map",
    "lowercase_sha256",
    "non_negative_integer",
    "opaque_exact",
    "provider_owned_opaque_ref",
}
_PROVIDER_SCOPES = {"global", "tenant", "workspace", "channel", "conversation", "thread"}
_EVENT_TYPES = {
    "membership.changed",
    "message.created",
    "message.deleted",
    "message.edited",
    "reaction.added",
    "reaction.removed",
}
_SIGNATURE_MODES = {"detached_raw_body", "transport_authenticated"}
_TIMESTAMP_MODES = {"signed_canonical_utc", "signed_unix_seconds"}
_NONCE_MODES = {"signed_unique"}
_ENDPOINT_BINDING_MODES = {"method_host_port_path_body", "transport_identity"}
_KEY_ROTATION_MODES = {"kid_routed", "single_active"}
_DEDUPE_SCOPES = {"tenant_workspace_provider_channel_event"}
_CURSOR_MODES = {"provider_durable", "native_backend_durable"}
_SEQUENCE_MODES = {"provider_monotonic", "native_backend_monotonic"}
_SNAPSHOT_MODES = {"provider_snapshot", "native_backend_snapshot"}
_FEATURES = {"attachments", "membership_events", "mentions", "threads"}
_OPERATIONS = {
    "add_reaction",
    "delete_message",
    "edit_message",
    "remove_reaction",
    "send_message",
}
_RETRY_AFTER_MODES = {"both", "delta_seconds", "http_date", "unavailable"}
_JSON_POINTER_ESCAPE_PATTERN = re.compile(r"~(?:0|1)")

_IDENTITY_MAPPING_FIELDS = {
    "schemaVersion",
    "canonicalField",
    "status",
    "providerJsonPointer",
    "mappingMode",
    "providerScope",
    "evidenceDigest",
}
_EVENT_MAPPING_FIELDS = {
    "schemaVersion",
    "eventType",
    "status",
    "providerEventType",
    "evidenceDigest",
}
_AUTHENTICATION_FIELDS = {
    "schemaVersion",
    "status",
    "verifierContractId",
    "signatureMode",
    "timestampMode",
    "nonceMode",
    "endpointBindingMode",
    "replayWindowSeconds",
    "keyRotationMode",
    "evidenceDigest",
}
_RESUME_FIELDS = {
    "schemaVersion",
    "status",
    "dedupeScope",
    "cursorMode",
    "sequenceMode",
    "snapshotMode",
    "eventIdRetentionSeconds",
    "cursorRetentionSeconds",
    "snapshotRetentionSeconds",
    "evidenceDigest",
}
_FEATURE_FIELDS = {"schemaVersion", "feature", "status", "evidenceDigest"}
_LIMIT_FIELDS = {
    "schemaVersion",
    "maxRawEventBytes",
    "maxRawPageBytes",
    "maxPageEvents",
    "maxTextBytes",
    "maxAttachments",
    "maxAttachmentBytes",
    "requestsPerWindow",
    "rateLimitWindowSeconds",
    "retryAfterMode",
    "evidenceDigest",
}
_OPERATION_FIELDS = {
    "schemaVersion",
    "operation",
    "status",
    "capability",
    "evidenceDigest",
}
_PROFILE_FIELDS = {
    "schemaVersion",
    "profileId",
    "revision",
    "observedAt",
    "tenantId",
    "workspaceId",
    "provider",
    "channelId",
    "environmentClass",
    "tenantMappingRevision",
    "serviceAccountParticipantId",
    "allowedConversationIds",
    "eventSchemaId",
    "eventSchemaVersion",
    "identityMappings",
    "eventMappings",
    "authentication",
    "resume",
    "features",
    "limits",
    "operations",
    "sourceEvidenceDigest",
}

_ProfileT = TypeVar("_ProfileT", bound="_ProviderProfileWireValue")


class _ProviderProfileWireValue:
    _MODEL_NAME: ClassVar[str]
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_PROFILE_COMPONENT_BYTES

    def to_dict(self) -> Dict[str, Any]:  # pragma: no cover - abstract boundary
        raise NotImplementedError

    @classmethod
    def from_dict(cls: Type[_ProfileT], value: object) -> _ProfileT:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def from_json_bytes(cls: Type[_ProfileT], encoded: object) -> _ProfileT:
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


def _require_exact_model(value: object, expected: type, label: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{label} must use the exact provider-profile V1 model class")


def _require_exact_tuple(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    return value


def _optional_id(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _id(value, label)


def _optional_digest(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _digest(value, label)


def _optional_positive_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _positive_integer(value, label)


def _json_pointer(value: object, label: str) -> str:
    pointer = _utf8_text(
        value,
        label,
        maximum_bytes=4_096,
        allow_empty=False,
        allow_message_controls=False,
    )
    if not pointer.startswith("/"):
        raise ValueError(f"{label} must be an absolute JSON pointer")
    index = 0
    while index < len(pointer):
        if pointer[index] != "~":
            index += 1
            continue
        match = _JSON_POINTER_ESCAPE_PATTERN.match(pointer, index)
        if match is None:
            raise ValueError(f"{label} contains a non-canonical JSON pointer escape")
        index = match.end()
    return pointer


def _check_three_state_details(
    *,
    status: str,
    details: tuple[object | None, ...],
    evidence_digest: str | None,
    label: str,
) -> None:
    _enum(status, _STATUSES, f"{label}.status")
    if status == "supported":
        if any(detail is None for detail in details) or evidence_digest is None:
            raise ValueError(f"supported {label} requires all details and evidence")
    elif status == "unsupported":
        if any(detail is not None for detail in details) or evidence_digest is None:
            raise ValueError(f"unsupported {label} requires no details and explicit evidence")
    elif any(detail is not None for detail in details) or evidence_digest is not None:
        raise ValueError(f"unverified {label} must not claim details or evidence")


@dataclass(frozen=True)
class IMProviderIdentityMappingV1(_ProviderProfileWireValue):
    schema_version: int
    canonical_field: str
    status: str
    provider_json_pointer: str | None = field(repr=False)
    mapping_mode: str | None
    provider_scope: str | None
    evidence_digest: str | None = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMProviderIdentityMappingV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMProviderIdentityMappingV1, "identity mapping")
        _schema_version(self.schema_version)
        _enum(self.canonical_field, _CANONICAL_FIELDS, "canonicalField")
        if self.provider_json_pointer is not None:
            _json_pointer(self.provider_json_pointer, "providerJsonPointer")
        if self.mapping_mode is not None:
            _enum(self.mapping_mode, _MAPPING_MODES, "mappingMode")
        if self.provider_scope is not None:
            _enum(self.provider_scope, _PROVIDER_SCOPES, "providerScope")
        _optional_digest(self.evidence_digest, "evidenceDigest")
        _check_three_state_details(
            status=self.status,
            details=(self.provider_json_pointer, self.mapping_mode, self.provider_scope),
            evidence_digest=self.evidence_digest,
            label="identity mapping",
        )
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "canonicalField": self.canonical_field,
            "status": self.status,
            "providerJsonPointer": self.provider_json_pointer,
            "mappingMode": self.mapping_mode,
            "providerScope": self.provider_scope,
            "evidenceDigest": self.evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMProviderIdentityMappingV1:
        if cls is not IMProviderIdentityMappingV1:
            raise TypeError("identity mapping decoder requires the exact V1 class")
        body = _plain_dict(value, _IDENTITY_MAPPING_FIELDS, "identity mapping")
        return cls(
            schema_version=body["schemaVersion"],
            canonical_field=body["canonicalField"],
            status=body["status"],
            provider_json_pointer=body["providerJsonPointer"],
            mapping_mode=body["mappingMode"],
            provider_scope=body["providerScope"],
            evidence_digest=body["evidenceDigest"],
        )


@dataclass(frozen=True)
class IMProviderEventMappingV1(_ProviderProfileWireValue):
    schema_version: int
    event_type: str
    status: str
    provider_event_type: str | None
    evidence_digest: str | None = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMProviderEventMappingV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMProviderEventMappingV1, "event mapping")
        _schema_version(self.schema_version)
        _enum(self.event_type, _EVENT_TYPES, "eventType")
        _optional_id(self.provider_event_type, "providerEventType")
        _optional_digest(self.evidence_digest, "evidenceDigest")
        _check_three_state_details(
            status=self.status,
            details=(self.provider_event_type,),
            evidence_digest=self.evidence_digest,
            label="event mapping",
        )
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "eventType": self.event_type,
            "status": self.status,
            "providerEventType": self.provider_event_type,
            "evidenceDigest": self.evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMProviderEventMappingV1:
        if cls is not IMProviderEventMappingV1:
            raise TypeError("event mapping decoder requires the exact V1 class")
        body = _plain_dict(value, _EVENT_MAPPING_FIELDS, "event mapping")
        return cls(
            schema_version=body["schemaVersion"],
            event_type=body["eventType"],
            status=body["status"],
            provider_event_type=body["providerEventType"],
            evidence_digest=body["evidenceDigest"],
        )


@dataclass(frozen=True)
class IMProviderAuthenticationProfileV1(_ProviderProfileWireValue):
    schema_version: int
    status: str
    verifier_contract_id: str | None
    signature_mode: str | None
    timestamp_mode: str | None
    nonce_mode: str | None
    endpoint_binding_mode: str | None
    replay_window_seconds: int | None
    key_rotation_mode: str | None
    evidence_digest: str | None = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMProviderAuthenticationProfileV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMProviderAuthenticationProfileV1, "authentication profile")
        _schema_version(self.schema_version)
        _optional_id(self.verifier_contract_id, "verifierContractId")
        for value, allowed, label in (
            (self.signature_mode, _SIGNATURE_MODES, "signatureMode"),
            (self.timestamp_mode, _TIMESTAMP_MODES, "timestampMode"),
            (self.nonce_mode, _NONCE_MODES, "nonceMode"),
            (self.endpoint_binding_mode, _ENDPOINT_BINDING_MODES, "endpointBindingMode"),
            (self.key_rotation_mode, _KEY_ROTATION_MODES, "keyRotationMode"),
        ):
            if value is not None:
                _enum(value, allowed, label)
        _optional_positive_integer(self.replay_window_seconds, "replayWindowSeconds")
        if (
            self.replay_window_seconds is not None
            and self.replay_window_seconds > _MAX_REPLAY_WINDOW_SECONDS
        ):
            raise ValueError("replayWindowSeconds exceeds the E2 safety bound")
        _optional_digest(self.evidence_digest, "evidenceDigest")
        _check_three_state_details(
            status=self.status,
            details=(
                self.verifier_contract_id,
                self.signature_mode,
                self.timestamp_mode,
                self.nonce_mode,
                self.endpoint_binding_mode,
                self.replay_window_seconds,
                self.key_rotation_mode,
            ),
            evidence_digest=self.evidence_digest,
            label="authentication profile",
        )
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "status": self.status,
            "verifierContractId": self.verifier_contract_id,
            "signatureMode": self.signature_mode,
            "timestampMode": self.timestamp_mode,
            "nonceMode": self.nonce_mode,
            "endpointBindingMode": self.endpoint_binding_mode,
            "replayWindowSeconds": self.replay_window_seconds,
            "keyRotationMode": self.key_rotation_mode,
            "evidenceDigest": self.evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMProviderAuthenticationProfileV1:
        if cls is not IMProviderAuthenticationProfileV1:
            raise TypeError("authentication profile decoder requires the exact V1 class")
        body = _plain_dict(value, _AUTHENTICATION_FIELDS, "authentication profile")
        return cls(
            schema_version=body["schemaVersion"],
            status=body["status"],
            verifier_contract_id=body["verifierContractId"],
            signature_mode=body["signatureMode"],
            timestamp_mode=body["timestampMode"],
            nonce_mode=body["nonceMode"],
            endpoint_binding_mode=body["endpointBindingMode"],
            replay_window_seconds=body["replayWindowSeconds"],
            key_rotation_mode=body["keyRotationMode"],
            evidence_digest=body["evidenceDigest"],
        )


@dataclass(frozen=True)
class IMProviderResumeProfileV1(_ProviderProfileWireValue):
    schema_version: int
    status: str
    dedupe_scope: str | None
    cursor_mode: str | None
    sequence_mode: str | None
    snapshot_mode: str | None
    event_id_retention_seconds: int | None
    cursor_retention_seconds: int | None
    snapshot_retention_seconds: int | None
    evidence_digest: str | None = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMProviderResumeProfileV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMProviderResumeProfileV1, "resume profile")
        _schema_version(self.schema_version)
        for value, allowed, label in (
            (self.dedupe_scope, _DEDUPE_SCOPES, "dedupeScope"),
            (self.cursor_mode, _CURSOR_MODES, "cursorMode"),
            (self.sequence_mode, _SEQUENCE_MODES, "sequenceMode"),
            (self.snapshot_mode, _SNAPSHOT_MODES, "snapshotMode"),
        ):
            if value is not None:
                _enum(value, allowed, label)
        for numeric_value, label in (
            (self.event_id_retention_seconds, "eventIdRetentionSeconds"),
            (self.cursor_retention_seconds, "cursorRetentionSeconds"),
            (self.snapshot_retention_seconds, "snapshotRetentionSeconds"),
        ):
            _optional_positive_integer(numeric_value, label)
        _optional_digest(self.evidence_digest, "evidenceDigest")
        _check_three_state_details(
            status=self.status,
            details=(
                self.dedupe_scope,
                self.cursor_mode,
                self.sequence_mode,
                self.snapshot_mode,
                self.event_id_retention_seconds,
                self.cursor_retention_seconds,
                self.snapshot_retention_seconds,
            ),
            evidence_digest=self.evidence_digest,
            label="resume profile",
        )
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "status": self.status,
            "dedupeScope": self.dedupe_scope,
            "cursorMode": self.cursor_mode,
            "sequenceMode": self.sequence_mode,
            "snapshotMode": self.snapshot_mode,
            "eventIdRetentionSeconds": self.event_id_retention_seconds,
            "cursorRetentionSeconds": self.cursor_retention_seconds,
            "snapshotRetentionSeconds": self.snapshot_retention_seconds,
            "evidenceDigest": self.evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMProviderResumeProfileV1:
        if cls is not IMProviderResumeProfileV1:
            raise TypeError("resume profile decoder requires the exact V1 class")
        body = _plain_dict(value, _RESUME_FIELDS, "resume profile")
        return cls(
            schema_version=body["schemaVersion"],
            status=body["status"],
            dedupe_scope=body["dedupeScope"],
            cursor_mode=body["cursorMode"],
            sequence_mode=body["sequenceMode"],
            snapshot_mode=body["snapshotMode"],
            event_id_retention_seconds=body["eventIdRetentionSeconds"],
            cursor_retention_seconds=body["cursorRetentionSeconds"],
            snapshot_retention_seconds=body["snapshotRetentionSeconds"],
            evidence_digest=body["evidenceDigest"],
        )


@dataclass(frozen=True)
class IMProviderFeatureV1(_ProviderProfileWireValue):
    schema_version: int
    feature: str
    status: str
    evidence_digest: str | None = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMProviderFeatureV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMProviderFeatureV1, "feature")
        _schema_version(self.schema_version)
        _enum(self.feature, _FEATURES, "feature")
        _optional_digest(self.evidence_digest, "evidenceDigest")
        _check_three_state_details(
            status=self.status,
            details=(),
            evidence_digest=self.evidence_digest,
            label="feature",
        )
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "feature": self.feature,
            "status": self.status,
            "evidenceDigest": self.evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMProviderFeatureV1:
        if cls is not IMProviderFeatureV1:
            raise TypeError("feature decoder requires the exact V1 class")
        body = _plain_dict(value, _FEATURE_FIELDS, "feature")
        return cls(
            schema_version=body["schemaVersion"],
            feature=body["feature"],
            status=body["status"],
            evidence_digest=body["evidenceDigest"],
        )


@dataclass(frozen=True)
class IMProviderLimitProfileV1(_ProviderProfileWireValue):
    schema_version: int
    max_raw_event_bytes: int
    max_raw_page_bytes: int
    max_page_events: int
    max_text_bytes: int
    max_attachments: int
    max_attachment_bytes: int
    requests_per_window: int | None
    rate_limit_window_seconds: int | None
    retry_after_mode: str
    evidence_digest: str = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMProviderLimitProfileV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMProviderLimitProfileV1, "limit profile")
        _schema_version(self.schema_version)
        for value, label, maximum in (
            (self.max_raw_event_bytes, "maxRawEventBytes", 3 * 1_024 * 1_024),
            (self.max_raw_page_bytes, "maxRawPageBytes", 16 * 1_024 * 1_024),
            (self.max_page_events, "maxPageEvents", 1_000),
        ):
            _positive_integer(value, label)
            if value > maximum:
                raise ValueError(f"{label} exceeds the frozen V1 limit")
        for value, label, maximum in (
            (self.max_text_bytes, "maxTextBytes", 1 * 1_024 * 1_024),
            (self.max_attachments, "maxAttachments", 64),
            (self.max_attachment_bytes, "maxAttachmentBytes", (1 << 63) - 1),
        ):
            _non_negative_integer(value, label)
            if value > maximum:
                raise ValueError(f"{label} exceeds the frozen V1 limit")
        _optional_positive_integer(self.requests_per_window, "requestsPerWindow")
        _optional_positive_integer(self.rate_limit_window_seconds, "rateLimitWindowSeconds")
        _enum(self.retry_after_mode, _RETRY_AFTER_MODES, "retryAfterMode")
        _digest(self.evidence_digest, "evidenceDigest")
        if (self.requests_per_window is None) != (self.rate_limit_window_seconds is None):
            raise ValueError("rate limit request and window bounds must be present together")
        if self.retry_after_mode == "unavailable":
            if self.requests_per_window is not None:
                raise ValueError("unavailable retry-after mode cannot claim a rate limit window")
        elif self.requests_per_window is None:
            raise ValueError("verified retry-after mode requires a rate limit window")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "maxRawEventBytes": self.max_raw_event_bytes,
            "maxRawPageBytes": self.max_raw_page_bytes,
            "maxPageEvents": self.max_page_events,
            "maxTextBytes": self.max_text_bytes,
            "maxAttachments": self.max_attachments,
            "maxAttachmentBytes": self.max_attachment_bytes,
            "requestsPerWindow": self.requests_per_window,
            "rateLimitWindowSeconds": self.rate_limit_window_seconds,
            "retryAfterMode": self.retry_after_mode,
            "evidenceDigest": self.evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMProviderLimitProfileV1:
        if cls is not IMProviderLimitProfileV1:
            raise TypeError("limit profile decoder requires the exact V1 class")
        body = _plain_dict(value, _LIMIT_FIELDS, "limit profile")
        return cls(
            schema_version=body["schemaVersion"],
            max_raw_event_bytes=body["maxRawEventBytes"],
            max_raw_page_bytes=body["maxRawPageBytes"],
            max_page_events=body["maxPageEvents"],
            max_text_bytes=body["maxTextBytes"],
            max_attachments=body["maxAttachments"],
            max_attachment_bytes=body["maxAttachmentBytes"],
            requests_per_window=body["requestsPerWindow"],
            rate_limit_window_seconds=body["rateLimitWindowSeconds"],
            retry_after_mode=body["retryAfterMode"],
            evidence_digest=body["evidenceDigest"],
        )


@dataclass(frozen=True)
class IMProviderOperationProfileV1(_ProviderProfileWireValue):
    schema_version: int
    operation: str
    status: str
    capability: IMOperationCapabilityV1 | None
    evidence_digest: str | None = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMProviderOperationProfileV1"

    def __post_init__(self) -> None:
        _require_exact_model(self, IMProviderOperationProfileV1, "operation profile")
        _schema_version(self.schema_version)
        _enum(self.operation, _OPERATIONS, "operation")
        if self.capability is not None:
            _require_exact_model(self.capability, IMOperationCapabilityV1, "capability")
            if self.capability.operation != self.operation:
                raise ValueError("operation capability must match the profiled operation")
        _optional_digest(self.evidence_digest, "evidenceDigest")
        _check_three_state_details(
            status=self.status,
            details=(self.capability,),
            evidence_digest=self.evidence_digest,
            label="operation profile",
        )
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "operation": self.operation,
            "status": self.status,
            "capability": None if self.capability is None else self.capability.to_dict(),
            "evidenceDigest": self.evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMProviderOperationProfileV1:
        if cls is not IMProviderOperationProfileV1:
            raise TypeError("operation profile decoder requires the exact V1 class")
        body = _plain_dict(value, _OPERATION_FIELDS, "operation profile")
        raw_capability = body["capability"]
        return cls(
            schema_version=body["schemaVersion"],
            operation=body["operation"],
            status=body["status"],
            capability=(
                None
                if raw_capability is None
                else IMOperationCapabilityV1.from_dict(raw_capability)
            ),
            evidence_digest=body["evidenceDigest"],
        )


@dataclass(frozen=True)
class IMProviderProfileV1(_ProviderProfileWireValue):
    schema_version: int
    profile_id: str
    revision: str
    observed_at: str
    tenant_id: str
    workspace_id: str
    provider: str
    channel_id: str
    environment_class: str
    tenant_mapping_revision: str
    service_account_participant_id: str
    allowed_conversation_ids: Tuple[str, ...]
    event_schema_id: str
    event_schema_version: str
    identity_mappings: Tuple[IMProviderIdentityMappingV1, ...] = field(repr=False)
    event_mappings: Tuple[IMProviderEventMappingV1, ...] = field(repr=False)
    authentication: IMProviderAuthenticationProfileV1 = field(repr=False)
    resume: IMProviderResumeProfileV1 = field(repr=False)
    features: Tuple[IMProviderFeatureV1, ...] = field(repr=False)
    limits: IMProviderLimitProfileV1 = field(repr=False)
    operations: Tuple[IMProviderOperationProfileV1, ...] = field(repr=False)
    source_evidence_digest: str | None = field(repr=False)

    _MODEL_NAME: ClassVar[str] = "IMProviderProfileV1"
    _MAX_CANONICAL_BYTES: ClassVar[int] = _MAX_PROFILE_BYTES

    def __post_init__(self) -> None:
        _require_exact_model(self, IMProviderProfileV1, "provider profile")
        _schema_version(self.schema_version)
        for value, label in (
            (self.profile_id, "profileId"),
            (self.revision, "revision"),
            (self.tenant_id, "tenantId"),
            (self.workspace_id, "workspaceId"),
            (self.provider, "provider"),
            (self.channel_id, "channelId"),
            (self.tenant_mapping_revision, "tenantMappingRevision"),
            (self.service_account_participant_id, "serviceAccountParticipantId"),
            (self.event_schema_id, "eventSchemaId"),
            (self.event_schema_version, "eventSchemaVersion"),
        ):
            _id(value, label)
        _timestamp(self.observed_at, "observedAt")
        _enum(self.environment_class, _ENVIRONMENT_CLASSES, "environmentClass")
        _require_exact_tuple(self.allowed_conversation_ids, "allowedConversationIds")
        if len(self.allowed_conversation_ids) > _MAX_ALLOWED_CONVERSATIONS:
            raise NativeIMCodecTooLargeError("allowedConversationIds exceeds its item limit")
        conversations = tuple(
            _id(value, f"allowedConversationIds[{index}]")
            for index, value in enumerate(self.allowed_conversation_ids)
        )
        _ordered_unique_text(conversations, "allowedConversationIds")
        self._validate_complete_table(
            self.identity_mappings,
            IMProviderIdentityMappingV1,
            "identityMappings",
            "canonical_field",
            _CANONICAL_FIELDS,
        )
        self._validate_complete_table(
            self.event_mappings,
            IMProviderEventMappingV1,
            "eventMappings",
            "event_type",
            _EVENT_TYPES,
        )
        _require_exact_model(
            self.authentication,
            IMProviderAuthenticationProfileV1,
            "authentication",
        )
        _require_exact_model(self.resume, IMProviderResumeProfileV1, "resume")
        self._validate_complete_table(
            self.features,
            IMProviderFeatureV1,
            "features",
            "feature",
            _FEATURES,
        )
        _require_exact_model(self.limits, IMProviderLimitProfileV1, "limits")
        self._validate_complete_table(
            self.operations,
            IMProviderOperationProfileV1,
            "operations",
            "operation",
            _OPERATIONS,
        )
        _optional_digest(self.source_evidence_digest, "sourceEvidenceDigest")
        self.canonical_bytes()

    @staticmethod
    def _validate_complete_table(
        table: object,
        expected_type: type,
        label: str,
        key_attribute: str,
        expected_keys: set[str],
    ) -> None:
        values = _require_exact_tuple(table, label)
        if len(values) != len(expected_keys):
            raise ValueError(f"{label} must contain the complete V1 table")
        for index, value in enumerate(values):
            _require_exact_model(value, expected_type, f"{label}[{index}]")
        keys = tuple(cast(str, getattr(value, key_attribute)) for value in values)
        _ordered_unique_text(keys, label)
        if set(keys) != expected_keys:
            raise ValueError(f"{label} must contain the complete V1 table")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "profileId": self.profile_id,
            "revision": self.revision,
            "observedAt": self.observed_at,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "provider": self.provider,
            "channelId": self.channel_id,
            "environmentClass": self.environment_class,
            "tenantMappingRevision": self.tenant_mapping_revision,
            "serviceAccountParticipantId": self.service_account_participant_id,
            "allowedConversationIds": list(self.allowed_conversation_ids),
            "eventSchemaId": self.event_schema_id,
            "eventSchemaVersion": self.event_schema_version,
            "identityMappings": [mapping.to_dict() for mapping in self.identity_mappings],
            "eventMappings": [mapping.to_dict() for mapping in self.event_mappings],
            "authentication": self.authentication.to_dict(),
            "resume": self.resume.to_dict(),
            "features": [feature.to_dict() for feature in self.features],
            "limits": self.limits.to_dict(),
            "operations": [operation.to_dict() for operation in self.operations],
            "sourceEvidenceDigest": self.source_evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> IMProviderProfileV1:
        if cls is not IMProviderProfileV1:
            raise TypeError("provider profile decoder requires the exact V1 class")
        body = _plain_dict(value, _PROFILE_FIELDS, "provider profile")
        conversations = _plain_list(
            body["allowedConversationIds"],
            "allowedConversationIds",
            maximum_items=_MAX_ALLOWED_CONVERSATIONS,
        )
        identity_mappings = _plain_list(
            body["identityMappings"],
            "identityMappings",
            maximum_items=len(_CANONICAL_FIELDS),
        )
        event_mappings = _plain_list(
            body["eventMappings"],
            "eventMappings",
            maximum_items=len(_EVENT_TYPES),
        )
        features = _plain_list(body["features"], "features", maximum_items=len(_FEATURES))
        operations = _plain_list(
            body["operations"],
            "operations",
            maximum_items=len(_OPERATIONS),
        )
        return cls(
            schema_version=body["schemaVersion"],
            profile_id=body["profileId"],
            revision=body["revision"],
            observed_at=body["observedAt"],
            tenant_id=body["tenantId"],
            workspace_id=body["workspaceId"],
            provider=body["provider"],
            channel_id=body["channelId"],
            environment_class=body["environmentClass"],
            tenant_mapping_revision=body["tenantMappingRevision"],
            service_account_participant_id=body["serviceAccountParticipantId"],
            allowed_conversation_ids=tuple(cast(list[str], conversations)),
            event_schema_id=body["eventSchemaId"],
            event_schema_version=body["eventSchemaVersion"],
            identity_mappings=tuple(
                IMProviderIdentityMappingV1.from_dict(item) for item in identity_mappings
            ),
            event_mappings=tuple(
                IMProviderEventMappingV1.from_dict(item) for item in event_mappings
            ),
            authentication=IMProviderAuthenticationProfileV1.from_dict(body["authentication"]),
            resume=IMProviderResumeProfileV1.from_dict(body["resume"]),
            features=tuple(IMProviderFeatureV1.from_dict(item) for item in features),
            limits=IMProviderLimitProfileV1.from_dict(body["limits"]),
            operations=tuple(IMProviderOperationProfileV1.from_dict(item) for item in operations),
            source_evidence_digest=body["sourceEvidenceDigest"],
        )


class IMProviderProfileBindingError(ValueError):
    """A caller-provided profile identity does not bind the exact trusted value."""


class IMProviderProfileNotReadyError(ValueError):
    """A valid profile lacks facts required by the E2 inbound-only boundary."""

    def __init__(self, blockers: Tuple[str, ...]) -> None:
        if type(blockers) is not tuple or not blockers:
            raise TypeError("profile readiness blockers must be a non-empty exact tuple")
        self.blockers = blockers
        super().__init__("native IM provider profile is not E2 ready")


class IMProviderProfileScopeError(ValueError):
    """A capability request does not match the complete pinned profile scope."""


_CORE_E2_IDENTITY_MAPPINGS = {
    "channelId",
    "conversationId",
    "cursor",
    "eventId",
    "participantId",
    "sequenceNumber",
    "snapshotToken",
    "tenantId",
    "workspaceId",
}
_MESSAGE_EVENT_TYPES = {"message.created", "message.deleted", "message.edited"}
_MESSAGE_REVISION_EVENT_TYPES = {"message.deleted", "message.edited"}
_REACTION_EVENT_TYPES = {"reaction.added", "reaction.removed"}


def evaluate_e2_profile_readiness_v1(profile: IMProviderProfileV1) -> Tuple[str, ...]:
    """Return stable fail-closed blockers without granting connection authority.

    The evaluator distinguishes a schema-valid provider record from one that has enough
    verified facts for the later E2 sandbox preflight.  An empty result is necessary but
    not sufficient to connect: deployment config, approval expiry, endpoint policy,
    credential purpose, kill-switch state, and transport gates remain separate.
    """

    _require_exact_model(profile, IMProviderProfileV1, "provider profile")
    blockers: list[str] = []
    if profile.environment_class != "sandbox":
        blockers.append("environment_not_sandbox")
    if not profile.allowed_conversation_ids:
        blockers.append("allowed_conversation_scope_empty")
    if profile.source_evidence_digest is None:
        blockers.append("source_evidence_unverified")
    if profile.authentication.status != "supported":
        blockers.append("authentication_not_supported")
    if profile.resume.status != "supported":
        blockers.append("resume_not_supported")

    identity_status = {
        mapping.canonical_field: mapping.status for mapping in profile.identity_mappings
    }
    event_status = {mapping.event_type: mapping.status for mapping in profile.event_mappings}
    feature_status = {feature.feature: feature.status for feature in profile.features}

    required_identity = set(_CORE_E2_IDENTITY_MAPPINGS)
    supported_events = {
        event_type for event_type, status in event_status.items() if status == "supported"
    }
    if not supported_events:
        blockers.append("event_mapping_none_supported")
    for event_type, status in event_status.items():
        if status == "unverified":
            blockers.append(f"event_mapping_unverified:{event_type}")
    if supported_events & (_MESSAGE_EVENT_TYPES | _REACTION_EVENT_TYPES):
        required_identity.add("messageId")
    if supported_events & _MESSAGE_REVISION_EVENT_TYPES:
        required_identity.add("messageRevision")
    if supported_events & _REACTION_EVENT_TYPES:
        required_identity.add("reactionKey")
    if "membership.changed" in supported_events:
        required_identity.add("membershipRevision")
    for feature_name, status in feature_status.items():
        if status == "unverified":
            blockers.append(f"feature_unverified:{feature_name}")
    if feature_status["attachments"] == "supported":
        required_identity.update(("attachmentId", "attachmentVersion"))
    if feature_status["threads"] == "supported":
        required_identity.add("threadId")
    for canonical_field in required_identity:
        if identity_status[canonical_field] != "supported":
            blockers.append(f"identity_mapping_not_supported:{canonical_field}")
    return tuple(sorted(set(blockers), key=lambda value: value.encode("utf-8")))


def validate_profile_binding_v1(
    profile: IMProviderProfileV1,
    *,
    expected_revision: str,
    expected_digest: str,
) -> None:
    """Require an exact revision and canonical digest without exposing either on failure."""

    _require_exact_model(profile, IMProviderProfileV1, "provider profile")
    _id(expected_revision, "expectedRevision")
    _digest(expected_digest, "expectedDigest")
    if profile.revision != expected_revision or profile.canonical_digest() != expected_digest:
        raise IMProviderProfileBindingError("native IM provider profile binding mismatch") from None


def derive_inbound_only_capability_snapshot_v1(
    profile: IMProviderProfileV1,
    request: IMCapabilityRequestV1,
    *,
    observed_at: str,
) -> IMCapabilitySnapshotV1:
    """Project verified inbound facts while mechanically omitting all outbound operations."""

    _require_exact_model(profile, IMProviderProfileV1, "provider profile")
    _require_exact_model(request, IMCapabilityRequestV1, "capability request")
    blockers = evaluate_e2_profile_readiness_v1(profile)
    if blockers:
        raise IMProviderProfileNotReadyError(blockers) from None
    profile_scope = (
        profile.tenant_id,
        profile.workspace_id,
        profile.provider,
        profile.channel_id,
    )
    request_scope = (
        request.tenant_id,
        request.workspace_id,
        request.provider,
        request.channel_id,
    )
    if profile_scope != request_scope:
        raise IMProviderProfileScopeError("native IM provider profile scope mismatch") from None
    feature_status = {feature.feature: feature.status for feature in profile.features}
    event_status = {mapping.event_type: mapping.status for mapping in profile.event_mappings}
    supports_attachments = feature_status["attachments"] == "supported"
    return IMCapabilitySnapshotV1(
        schema_version=NATIVE_IM_SCHEMA_VERSION,
        tenant_id=profile.tenant_id,
        workspace_id=profile.workspace_id,
        provider=profile.provider,
        channel_id=profile.channel_id,
        revision=profile.revision,
        observed_at=observed_at,
        operations=(),
        idempotency_retention_seconds=None,
        supports_threads=feature_status["threads"] == "supported",
        supports_mentions=feature_status["mentions"] == "supported",
        supports_attachments=supports_attachments,
        supports_membership_events=(
            feature_status["membership_events"] == "supported"
            and event_status["membership.changed"] == "supported"
        ),
        max_text_bytes=profile.limits.max_text_bytes,
        max_attachments=profile.limits.max_attachments if supports_attachments else 0,
        max_attachment_bytes=(profile.limits.max_attachment_bytes if supports_attachments else 0),
    )


__all__ = [
    "IMProviderAuthenticationProfileV1",
    "IMProviderEventMappingV1",
    "IMProviderFeatureV1",
    "IMProviderIdentityMappingV1",
    "IMProviderLimitProfileV1",
    "IMProviderOperationProfileV1",
    "IMProviderProfileBindingError",
    "IMProviderProfileNotReadyError",
    "IMProviderProfileScopeError",
    "IMProviderProfileV1",
    "IMProviderResumeProfileV1",
    "NATIVE_IM_SCHEMA_VERSION",
    "derive_inbound_only_capability_snapshot_v1",
    "evaluate_e2_profile_readiness_v1",
    "validate_profile_binding_v1",
]
