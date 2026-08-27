# ruff: noqa: UP006, UP035
"""Strict, side-effect-free codecs for scoped invocation results.

These immutable values describe a proposed result and its Artifact identities.  They are
not durable receipts, do not authorize completion, and never carry a plaintext lease.  A
future EventStore acceptor must revalidate them against one exact scoped start claim and
commit the complete receipt graph before any result becomes authoritative.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, Mapping, Set, Tuple, cast

from ._artifact_codec import (
    MAX_ARTIFACT_IDENTITY_CHARACTERS,
    CanonicalArtifactMetadataV1,
    artifact_blob_digest_v1,
    artifact_metadata_digest_v1,
    artifact_request_digest_v1,
    canonical_artifact_metadata_v1,
    decode_canonical_artifact_metadata_v1,
)
from .invocation_execution import (
    CANONICAL_ORCHESTRATOR_ACTOR_ID,
    EffectClass,
    ScopedInvocationStartReceiptV3,
)
from .protocol import TaskStatus

SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION = 2
SCOPED_INVOCATION_RESULT_EVIDENCE_SCHEMA_VERSION = 2
SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_SCHEMA_VERSION = 2
SCOPED_INVOCATION_RESULT_TERMINAL_TRANSITION_SCHEMA_VERSION = 2
SCOPED_INVOCATION_RESULT_RECEIPT_SCHEMA_VERSION = 2
SCOPED_INVOCATION_RESULT_MANIFEST_DOMAIN = "quantum-entanglement.invocation-result-manifest/2\n"
ACTION_RECEIPT_SET_DOMAIN = "quantum-entanglement.action-receipt-set/1\n"
SCOPED_INVOCATION_RESULT_ARTIFACT_CANDIDATE_DOMAIN = (
    "quantum-entanglement.invocation-result-artifact-candidate/2\n"
)
SCOPED_INVOCATION_RESULT_EVIDENCE_DOMAIN = "quantum-entanglement.invocation-result-evidence/2\n"
SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_DOMAIN = (
    "quantum-entanglement.invocation-result-acceptance-request/2\n"
)
SCOPED_INVOCATION_START_RECEIPT_DIGEST_DOMAIN = "quantum-entanglement.invocation-start-receipt/3\n"
SCOPED_INVOCATION_RESULT_TERMINAL_TRANSITION_DOMAIN = (
    "quantum-entanglement.invocation-result-terminal-transition/2\n"
)
SCOPED_INVOCATION_RESULT_RECEIPT_DOMAIN = "quantum-entanglement.invocation-result-receipt/2\n"
TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE = "task.invocation.result.accepted"
TASK_STATUS_CHANGED_EVENT_TYPE = "task.status.changed"
EMPTY_ACTION_RECEIPT_SET_DIGEST = hashlib.sha256(
    ACTION_RECEIPT_SET_DOMAIN.encode("utf-8") + b"[]"
).hexdigest()

_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MIN_SQLITE_INTEGER = -(1 << 63)
_MAX_IDENTITY_BYTES = 4_096
_MAX_MEDIA_TYPE_BYTES = 255
_MAX_ARTIFACTS = 256
_MAX_ARTIFACT_CONTENT_BYTES = 16 * 1024 * 1024
_MAX_RESULT_CONTENT_BYTES = 64 * 1024 * 1024
_MAX_RESULT_ARTIFACT_METADATA_BYTES = 1_048_576
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_NARRATION_BYTES = 524_288
_MAX_RESULT_METADATA_BYTES = 65_536
_MAX_METADATA_DEPTH = 64
_MAX_METADATA_NODES = 10_000
_MAX_METADATA_KEY_BYTES = 4_096
_MAX_METADATA_STRING_BYTES = 262_144
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_BLOB_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CANONICAL_UTC_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_MAPPING_PROXY_TYPE: type = type(MappingProxyType({}))

_ARTIFACT_FIELDS = frozenset(
    (
        "artifactId",
        "name",
        "version",
        "parentVersion",
        "mediaType",
        "blobDigest",
        "byteSize",
        "metadataDigest",
        "createdBy",
        "idempotencyKey",
        "requestDigest",
    )
)

_MANIFEST_FIELDS = frozenset(
    (
        "schemaVersion",
        "tenantId",
        "workspaceId",
        "invocationId",
        "sessionId",
        "planId",
        "taskId",
        "agentId",
        "jobIdempotencyKey",
        "taskRevision",
        "correlationId",
        "causationId",
        "runtimeRevision",
        "executionManifestDigest",
        "effectClass",
        "actionReceiptSetDigest",
        "resultRef",
        "narration",
        "metadata",
        "primaryArtifactId",
        "artifacts",
    )
)

_EVENT_COORDINATE_FIELDS = frozenset(
    (
        "eventId",
        "streamId",
        "eventType",
        "sequence",
        "globalPosition",
        "eventEnvelopeDigest",
    )
)

_RESULT_EVIDENCE_FIELDS = frozenset(
    (
        "schemaVersion",
        "evidenceKind",
        "receiptId",
        "tenantId",
        "workspaceId",
        "invocationId",
        "sessionId",
        "planId",
        "taskId",
        "agentId",
        "jobIdempotencyKey",
        "runningTaskRevision",
        "terminalTaskRevision",
        "attemptId",
        "attemptNumber",
        "leaseEpoch",
        "workerId",
        "leaseTokenDigest",
        "startReceiptDigest",
        "executionManifestDigest",
        "resultManifestSchemaVersion",
        "resultManifestDigest",
        "resultRef",
        "effectClass",
        "actionReceiptSetDigest",
        "acceptanceIdempotencyKey",
        "requestDigest",
        "acceptedAt",
        "artifactCount",
    )
)

_TERMINAL_TRANSITION_FIELDS = frozenset(
    (
        "schemaVersion",
        "transitionKind",
        "tenantId",
        "workspaceId",
        "invocationId",
        "sessionId",
        "planId",
        "taskId",
        "agentId",
        "jobIdempotencyKey",
        "runtimeRevision",
        "correlationId",
        "previous",
        "current",
        "reason",
        "runningTaskRevision",
        "terminalTaskRevision",
        "resultReceiptId",
        "resultEventId",
        "resultEvidenceDigest",
    )
)

_RESULT_RECEIPT_FIELDS = frozenset(
    (
        "schemaVersion",
        "receiptId",
        "startReceipt",
        "evidence",
        "resultEvent",
        "terminalEvent",
        "terminalTransition",
        "receiptDigest",
    )
)


def _exact_dict(value: object, fields: Set[str], label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a plain dictionary")
    typed = cast(Dict[str, Any], value)
    keys = tuple(typed)
    if any(type(key) is not str for key in keys):
        raise TypeError(f"{label} keys must be plain strings")
    if set(keys) != fields:
        raise ValueError(f"{label} fields do not match its exact schema version")
    return typed


def _text(
    value: object,
    label: str,
    *,
    maximum_bytes: int = _MAX_IDENTITY_BYTES,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a plain string")
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty without surrounding whitespace")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        encoded = None
    if encoded is None:
        raise ValueError(f"{label} must be valid UTF-8") from None
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{label} exceeds its UTF-8 byte limit")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{label} contains a C0 or DEL control character")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must use Unicode NFC")
    return value


def _digest(value: object, label: str) -> str:
    digest = _text(value, label, maximum_bytes=64)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} must be canonical lowercase SHA-256")
    return digest


def _timestamp(value: object, label: str) -> str:
    timestamp = _text(value, label, maximum_bytes=27)
    if _CANONICAL_UTC_PATTERN.fullmatch(timestamp) is None:
        raise ValueError(f"{label} must be canonical UTC with microseconds")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValueError(f"{label} must be a valid UTC timestamp") from None
    canonical = (
        parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    if canonical != timestamp:
        raise ValueError(f"{label} must be canonical UTC with microseconds")
    return timestamp


def _body_text(value: object, label: str, *, maximum_bytes: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a plain string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        encoded = None
    if encoded is None:
        raise ValueError(f"{label} must be valid UTF-8") from None
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{label} exceeds its UTF-8 byte limit")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must use Unicode NFC")
    if any(
        (ord(character) < 0x20 and character not in "\t\n") or ord(character) == 0x7F
        for character in value
    ):
        raise ValueError(f"{label} contains a forbidden control character")
    return value


def _same_snapshot_slot(current: object, captured: object) -> bool:
    if type(current) is not type(captured):
        return False
    if type(current) in (dict, list):
        return current is captured
    return current == captured


def _snapshot_json_value(
    value: object,
    label: str,
    *,
    depth: int,
    active: set[int],
    nodes: list[int],
) -> object:
    nodes[0] += 1
    if nodes[0] > _MAX_METADATA_NODES:
        raise ValueError(f"{label} exceeds its JSON node limit")
    value_type = type(value)
    if value is None or value_type is bool:
        return value
    if value_type is str:
        return _body_text(value, label, maximum_bytes=_MAX_METADATA_STRING_BYTES)
    if value_type is int:
        integer = cast(int, value)
        if not _MIN_SQLITE_INTEGER <= integer <= _MAX_SQLITE_INTEGER:
            raise ValueError(f"{label} integer is outside the signed 64-bit range")
        return integer
    if value_type is float:
        number = cast(float, value)
        if not math.isfinite(number):
            raise ValueError(f"{label} contains a non-finite number")
        return number
    if value_type not in (dict, list):
        raise TypeError(f"{label} contains a non-JSON value")
    if depth >= _MAX_METADATA_DEPTH:
        raise ValueError(f"{label} exceeds its nesting limit")
    identity = id(value)
    if identity in active:
        raise ValueError(f"{label} contains a reference cycle")
    active.add(identity)
    try:
        if value_type is list:
            sequence = cast(list[object], value)
            try:
                items = tuple(sequence)
            except RuntimeError as error:
                raise ValueError(f"{label} changed while it was being snapshotted") from error
            copied = [
                _snapshot_json_value(
                    item,
                    f"{label}[{index}]",
                    depth=depth + 1,
                    active=active,
                    nodes=nodes,
                )
                for index, item in enumerate(items)
            ]
            try:
                current_items = tuple(sequence)
            except RuntimeError as error:
                raise ValueError(f"{label} changed while it was being snapshotted") from error
            if len(current_items) != len(items) or any(
                not _same_snapshot_slot(current, captured)
                for current, captured in zip(current_items, items)
            ):
                raise ValueError(f"{label} changed while it was being snapshotted")
            return copied

        mapping = cast(dict[object, object], value)
        try:
            entries = tuple(mapping.items())
        except RuntimeError as error:
            raise ValueError(f"{label} changed while it was being snapshotted") from error
        copied_mapping: dict[str, object] = {}
        for key, item in entries:
            nodes[0] += 1
            if nodes[0] > _MAX_METADATA_NODES:
                raise ValueError(f"{label} exceeds its JSON node limit")
            if type(key) is not str:
                raise TypeError(f"{label} keys must be plain strings")
            normalized_key = _body_text(
                key,
                f"{label} key",
                maximum_bytes=_MAX_METADATA_KEY_BYTES,
            )
            copied_mapping[normalized_key] = _snapshot_json_value(
                item,
                f"{label}.{normalized_key}",
                depth=depth + 1,
                active=active,
                nodes=nodes,
            )
        try:
            current_entries = tuple(mapping.items())
        except RuntimeError as error:
            raise ValueError(f"{label} changed while it was being snapshotted") from error
        if len(current_entries) != len(entries) or any(
            current_key != captured_key or not _same_snapshot_slot(current_value, captured_value)
            for (current_key, current_value), (captured_key, captured_value) in zip(
                current_entries,
                entries,
            )
        ):
            raise ValueError(f"{label} changed while it was being snapshotted")
        return copied_mapping
    finally:
        active.discard(identity)


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        return MappingProxyType({key: _freeze_json(item) for key, item in mapping.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in cast(list[object], value))
    return value


def _thaw_json(value: object) -> object:
    if type(value) is _MAPPING_PROXY_TYPE:
        mapping = cast(Mapping[str, object], value)
        return {key: _thaw_json(item) for key, item in mapping.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in cast(tuple[object, ...], value)]
    return value


def _snapshot_json_object(
    value: object,
    label: str,
    *,
    allow_frozen: bool,
) -> tuple[Mapping[str, object], bytes]:
    if allow_frozen and type(value) is _MAPPING_PROXY_TYPE:
        value = _thaw_json(value)
    if type(value) is not dict:
        raise TypeError(f"{label} must be a plain dictionary")
    copied = _snapshot_json_value(value, label, depth=0, active=set(), nodes=[0])
    if type(copied) is not dict:  # pragma: no cover - protected by the exact root guard.
        raise TypeError(f"{label} must be a JSON object")
    canonical = _canonical_json_bytes(cast(Mapping[str, object], copied))
    if len(canonical) > _MAX_RESULT_METADATA_BYTES:
        raise ValueError(f"{label} exceeds its canonical byte limit")
    return cast(Mapping[str, object], _freeze_json(copied)), canonical


def _blob_digest(value: object) -> str:
    digest = _text(value, "blobDigest", maximum_bytes=71)
    if _BLOB_DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError("blobDigest must be canonical sha256:<lowercase-hex>")
    return digest


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    if value <= 0 or value > _MAX_SQLITE_INTEGER:
        raise ValueError(f"{label} is outside its supported range")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    if value < 0 or value > _MAX_SQLITE_INTEGER:
        raise ValueError(f"{label} is outside its supported range")
    return value


def _effect_class(value: object) -> EffectClass:
    if type(value) is not str:
        raise TypeError("effectClass must be a plain string")
    try:
        return EffectClass(value)
    except ValueError:
        raise ValueError("effectClass is unsupported") from None


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True)
class ScopedInvocationResultArtifactV2:
    """One immutable Artifact version descriptor covered by a result manifest."""

    artifact_id: str
    name: str
    version: int
    parent_version: int | None
    media_type: str
    blob_digest: str
    byte_size: int
    metadata_digest: str
    created_by: str
    idempotency_key: str
    request_digest: str

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationResultArtifactV2:
            raise TypeError("artifact descriptor must be an exact ScopedInvocationResultArtifactV2")
        for label, value in (
            ("artifactId", self.artifact_id),
            ("name", self.name),
            ("createdBy", self.created_by),
            ("idempotencyKey", self.idempotency_key),
        ):
            _text(value, label)
        version = _positive_integer(self.version, "version")
        if self.parent_version is None:
            if version != 1:
                raise ValueError("parentVersion is required after artifact version 1")
        else:
            parent = _positive_integer(self.parent_version, "parentVersion")
            if parent != version - 1:
                raise ValueError("parentVersion must immediately precede version")
        media_type = _text(
            self.media_type,
            "mediaType",
            maximum_bytes=_MAX_MEDIA_TYPE_BYTES,
        )
        if any(character.isspace() for character in media_type) or "/" not in media_type:
            raise ValueError("mediaType must be a whitespace-free type/subtype")
        _blob_digest(self.blob_digest)
        _nonnegative_integer(self.byte_size, "byteSize")
        _digest(self.metadata_digest, "metadataDigest")
        _digest(self.request_digest, "requestDigest")

    def to_dict(self) -> Dict[str, object]:
        ScopedInvocationResultArtifactV2.__post_init__(self)
        return {
            "artifactId": self.artifact_id,
            "name": self.name,
            "version": self.version,
            "parentVersion": self.parent_version,
            "mediaType": self.media_type,
            "blobDigest": self.blob_digest,
            "byteSize": self.byte_size,
            "metadataDigest": self.metadata_digest,
            "createdBy": self.created_by,
            "idempotencyKey": self.idempotency_key,
            "requestDigest": self.request_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> ScopedInvocationResultArtifactV2:
        if cls is not ScopedInvocationResultArtifactV2:
            raise TypeError("artifact decoder requires the exact schema-2 class")
        raw = _exact_dict(value, set(_ARTIFACT_FIELDS), "result artifact descriptor")
        return cls(
            artifact_id=raw["artifactId"],
            name=raw["name"],
            version=raw["version"],
            parent_version=raw["parentVersion"],
            media_type=raw["mediaType"],
            blob_digest=raw["blobDigest"],
            byte_size=raw["byteSize"],
            metadata_digest=raw["metadataDigest"],
            created_by=raw["createdBy"],
            idempotency_key=raw["idempotencyKey"],
            request_digest=raw["requestDigest"],
        )


@dataclass(frozen=True)
class ScopedInvocationResultArtifactCandidateV2:
    """Exact in-process Artifact content proposal; never a serializable receipt."""

    tenant_id: str
    workspace_id: str
    session_id: str
    task_id: str
    artifact_id: str
    name: str
    media_type: str
    content: bytes = field(repr=False)
    metadata_canonical_bytes: bytes = field(repr=False)
    created_by: str
    idempotency_key: str
    expected_head_version: int
    _metadata: CanonicalArtifactMetadataV1 = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationResultArtifactCandidateV2:
            raise TypeError(
                "artifact candidate must be an exact ScopedInvocationResultArtifactCandidateV2"
            )
        for label, value in (
            ("tenantId", self.tenant_id),
            ("workspaceId", self.workspace_id),
            ("sessionId", self.session_id),
            ("taskId", self.task_id),
            ("artifactId", self.artifact_id),
            ("name", self.name),
            ("createdBy", self.created_by),
            ("idempotencyKey", self.idempotency_key),
        ):
            _text(value, label)
        media_type = _text(
            self.media_type,
            "mediaType",
            maximum_bytes=_MAX_MEDIA_TYPE_BYTES,
        )
        if any(character.isspace() for character in media_type) or "/" not in media_type:
            raise ValueError("mediaType must be a whitespace-free type/subtype")
        if type(self.content) is not bytes:
            raise TypeError("artifact content must be immutable bytes")
        if len(self.content) > _MAX_ARTIFACT_CONTENT_BYTES:
            raise ValueError("artifact content exceeds its byte limit")
        metadata = decode_canonical_artifact_metadata_v1(self.metadata_canonical_bytes)
        expected_head = _nonnegative_integer(self.expected_head_version, "expectedHeadVersion")
        if expected_head >= _MAX_SQLITE_INTEGER:
            raise ValueError("expectedHeadVersion cannot allocate a successor version")
        object.__setattr__(self, "_metadata", metadata)

    @classmethod
    def from_content_metadata(
        cls,
        *,
        tenant_id: str,
        workspace_id: str,
        session_id: str,
        task_id: str,
        artifact_id: str,
        name: str,
        media_type: str,
        content: bytes,
        metadata: object,
        created_by: str,
        idempotency_key: str,
        expected_head_version: int,
    ) -> ScopedInvocationResultArtifactCandidateV2:
        if cls is not ScopedInvocationResultArtifactCandidateV2:
            raise TypeError("artifact candidate factory requires the exact schema-2 class")
        canonical_metadata = canonical_artifact_metadata_v1(metadata)
        return cls(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            session_id=session_id,
            task_id=task_id,
            artifact_id=artifact_id,
            name=name,
            media_type=media_type,
            content=content,
            metadata_canonical_bytes=canonical_metadata.canonical_bytes,
            created_by=created_by,
            idempotency_key=idempotency_key,
            expected_head_version=expected_head_version,
        )

    @property
    def version(self) -> int:
        ScopedInvocationResultArtifactCandidateV2.__post_init__(self)
        return self.expected_head_version + 1

    @property
    def parent_version(self) -> int | None:
        ScopedInvocationResultArtifactCandidateV2.__post_init__(self)
        return self.expected_head_version or None

    @property
    def byte_size(self) -> int:
        ScopedInvocationResultArtifactCandidateV2.__post_init__(self)
        return len(self.content)

    @property
    def blob_digest(self) -> str:
        ScopedInvocationResultArtifactCandidateV2.__post_init__(self)
        return artifact_blob_digest_v1(self.content)

    @property
    def metadata_digest(self) -> str:
        ScopedInvocationResultArtifactCandidateV2.__post_init__(self)
        return artifact_metadata_digest_v1(self._metadata)

    @property
    def artifact_request_digest(self) -> str:
        ScopedInvocationResultArtifactCandidateV2.__post_init__(self)
        return artifact_request_digest_v1(
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            task_id=self.task_id,
            name=self.name,
            media_type=self.media_type,
            blob_digest=self.blob_digest,
            byte_size=self.byte_size,
            metadata=self._metadata,
            created_by=self.created_by,
        )

    def metadata_dict(self) -> Dict[str, object]:
        ScopedInvocationResultArtifactCandidateV2.__post_init__(self)
        return self._metadata.to_dict()

    def to_descriptor(self) -> ScopedInvocationResultArtifactV2:
        ScopedInvocationResultArtifactCandidateV2.__post_init__(self)
        return ScopedInvocationResultArtifactV2(
            artifact_id=self.artifact_id,
            name=self.name,
            version=self.version,
            parent_version=self.parent_version,
            media_type=self.media_type,
            blob_digest=self.blob_digest,
            byte_size=self.byte_size,
            metadata_digest=self.metadata_digest,
            created_by=self.created_by,
            idempotency_key=self.idempotency_key,
            request_digest=self.artifact_request_digest,
        )

    def _identity_dict(self) -> Dict[str, object]:
        return {
            "schemaVersion": 2,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "sessionId": self.session_id,
            "taskId": self.task_id,
            "artifactId": self.artifact_id,
            "name": self.name,
            "mediaType": self.media_type,
            "blobDigest": self.blob_digest,
            "byteSize": self.byte_size,
            "metadataDigest": self.metadata_digest,
            "createdBy": self.created_by,
            "idempotencyKey": self.idempotency_key,
            "expectedHeadVersion": self.expected_head_version,
            "version": self.version,
            "parentVersion": self.parent_version,
            "artifactRequestDigest": self.artifact_request_digest,
        }

    def canonical_digest(self) -> str:
        ScopedInvocationResultArtifactCandidateV2.__post_init__(self)
        return hashlib.sha256(
            SCOPED_INVOCATION_RESULT_ARTIFACT_CANDIDATE_DOMAIN.encode("utf-8")
            + _canonical_json_bytes(self._identity_dict())
        ).hexdigest()


@dataclass(frozen=True)
class ScopedInvocationResultEventCoordinatesV2:
    """Immutable coordinates and digest for one event row stored by result acceptance."""

    event_id: str
    stream_id: str
    event_type: str
    sequence: int
    global_position: int
    event_envelope_digest: str

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationResultEventCoordinatesV2:
            raise TypeError(
                "event coordinates must be exact ScopedInvocationResultEventCoordinatesV2"
            )
        for label, value in (
            ("eventId", self.event_id),
            ("streamId", self.stream_id),
            ("eventType", self.event_type),
        ):
            _text(value, label)
        sequence = _positive_integer(self.sequence, "sequence")
        global_position = _positive_integer(self.global_position, "globalPosition")
        if global_position < sequence:
            raise ValueError("globalPosition must not precede its stream sequence")
        _digest(self.event_envelope_digest, "eventEnvelopeDigest")

    def to_dict(self) -> Dict[str, object]:
        ScopedInvocationResultEventCoordinatesV2.__post_init__(self)
        return {
            "eventId": self.event_id,
            "streamId": self.stream_id,
            "eventType": self.event_type,
            "sequence": self.sequence,
            "globalPosition": self.global_position,
            "eventEnvelopeDigest": self.event_envelope_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> ScopedInvocationResultEventCoordinatesV2:
        if cls is not ScopedInvocationResultEventCoordinatesV2:
            raise TypeError("event coordinate decoder requires the exact schema-2 class")
        raw = _exact_dict(value, set(_EVENT_COORDINATE_FIELDS), "result event coordinates")
        return cls(
            event_id=raw["eventId"],
            stream_id=raw["streamId"],
            event_type=raw["eventType"],
            sequence=raw["sequence"],
            global_position=raw["globalPosition"],
            event_envelope_digest=raw["eventEnvelopeDigest"],
        )


def _result_event_coordinates_snapshot(
    coordinates: object,
) -> ScopedInvocationResultEventCoordinatesV2:
    if type(coordinates) is not ScopedInvocationResultEventCoordinatesV2:
        raise TypeError("coordinates must be exact ScopedInvocationResultEventCoordinatesV2")
    return ScopedInvocationResultEventCoordinatesV2.from_dict(
        ScopedInvocationResultEventCoordinatesV2.to_dict(coordinates)
    )


@dataclass(frozen=True)
class ScopedInvocationResultEvidenceV2:
    """Exact schema-2 payload for the canonical accepted-result event."""

    schema_version: int
    evidence_kind: str
    receipt_id: str
    tenant_id: str
    workspace_id: str
    invocation_id: str
    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    job_idempotency_key: str
    running_task_revision: int
    terminal_task_revision: int
    attempt_id: str
    attempt_number: int
    lease_epoch: int
    worker_id: str
    lease_token_digest: str
    start_receipt_digest: str
    execution_manifest_digest: str
    result_manifest_schema_version: int
    result_manifest_digest: str
    result_ref: str
    effect_class: EffectClass
    action_receipt_set_digest: str
    acceptance_idempotency_key: str
    request_digest: str
    accepted_at: str
    artifact_count: int

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationResultEvidenceV2:
            raise TypeError("result evidence must be exact ScopedInvocationResultEvidenceV2")
        if type(self.schema_version) is not int:
            raise TypeError("schemaVersion must be an exact integer")
        if self.schema_version != SCOPED_INVOCATION_RESULT_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("schemaVersion is unsupported")
        if type(self.evidence_kind) is not str:
            raise TypeError("evidenceKind must be a plain string")
        if self.evidence_kind != "attempt_bound":
            raise ValueError("evidenceKind must be attempt_bound")
        for label, value in (
            ("receiptId", self.receipt_id),
            ("tenantId", self.tenant_id),
            ("workspaceId", self.workspace_id),
            ("invocationId", self.invocation_id),
            ("sessionId", self.session_id),
            ("planId", self.plan_id),
            ("taskId", self.task_id),
            ("agentId", self.agent_id),
            ("jobIdempotencyKey", self.job_idempotency_key),
            ("attemptId", self.attempt_id),
            ("workerId", self.worker_id),
            ("resultRef", self.result_ref),
            ("acceptanceIdempotencyKey", self.acceptance_idempotency_key),
        ):
            _text(value, label)
        running_revision = _positive_integer(
            self.running_task_revision,
            "runningTaskRevision",
        )
        terminal_revision = _positive_integer(
            self.terminal_task_revision,
            "terminalTaskRevision",
        )
        if running_revision >= _MAX_SQLITE_INTEGER or terminal_revision != running_revision + 1:
            raise ValueError("terminalTaskRevision must immediately follow runningTaskRevision")
        _positive_integer(self.attempt_number, "attemptNumber")
        _positive_integer(self.lease_epoch, "leaseEpoch")
        for label, value in (
            ("leaseTokenDigest", self.lease_token_digest),
            ("startReceiptDigest", self.start_receipt_digest),
            ("executionManifestDigest", self.execution_manifest_digest),
            ("resultManifestDigest", self.result_manifest_digest),
            ("actionReceiptSetDigest", self.action_receipt_set_digest),
            ("requestDigest", self.request_digest),
        ):
            _digest(value, label)
        if type(self.result_manifest_schema_version) is not int:
            raise TypeError("resultManifestSchemaVersion must be an exact integer")
        if self.result_manifest_schema_version != SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION:
            raise ValueError("resultManifestSchemaVersion is unsupported")
        if type(self.effect_class) is not EffectClass:
            raise TypeError("effectClass must be an exact EffectClass")
        if self.effect_class is not EffectClass.PURE:
            raise ValueError("accepted schema-2 evidence requires effectClass pure")
        if self.action_receipt_set_digest != EMPTY_ACTION_RECEIPT_SET_DIGEST:
            raise ValueError("accepted pure evidence requires the empty action receipt set")
        _timestamp(self.accepted_at, "acceptedAt")
        artifact_count = _nonnegative_integer(self.artifact_count, "artifactCount")
        if artifact_count > _MAX_ARTIFACTS:
            raise ValueError("artifactCount exceeds the schema-2 limit")

    def to_dict(self) -> Dict[str, object]:
        ScopedInvocationResultEvidenceV2.__post_init__(self)
        return {
            "schemaVersion": self.schema_version,
            "evidenceKind": self.evidence_kind,
            "receiptId": self.receipt_id,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "invocationId": self.invocation_id,
            "sessionId": self.session_id,
            "planId": self.plan_id,
            "taskId": self.task_id,
            "agentId": self.agent_id,
            "jobIdempotencyKey": self.job_idempotency_key,
            "runningTaskRevision": self.running_task_revision,
            "terminalTaskRevision": self.terminal_task_revision,
            "attemptId": self.attempt_id,
            "attemptNumber": self.attempt_number,
            "leaseEpoch": self.lease_epoch,
            "workerId": self.worker_id,
            "leaseTokenDigest": self.lease_token_digest,
            "startReceiptDigest": self.start_receipt_digest,
            "executionManifestDigest": self.execution_manifest_digest,
            "resultManifestSchemaVersion": self.result_manifest_schema_version,
            "resultManifestDigest": self.result_manifest_digest,
            "resultRef": self.result_ref,
            "effectClass": self.effect_class.value,
            "actionReceiptSetDigest": self.action_receipt_set_digest,
            "acceptanceIdempotencyKey": self.acceptance_idempotency_key,
            "requestDigest": self.request_digest,
            "acceptedAt": self.accepted_at,
            "artifactCount": self.artifact_count,
        }

    @classmethod
    def from_dict(cls, value: object) -> ScopedInvocationResultEvidenceV2:
        if cls is not ScopedInvocationResultEvidenceV2:
            raise TypeError("result evidence decoder requires the exact schema-2 class")
        raw = _exact_dict(value, set(_RESULT_EVIDENCE_FIELDS), "scoped result evidence")
        return cls(
            schema_version=raw["schemaVersion"],
            evidence_kind=raw["evidenceKind"],
            receipt_id=raw["receiptId"],
            tenant_id=raw["tenantId"],
            workspace_id=raw["workspaceId"],
            invocation_id=raw["invocationId"],
            session_id=raw["sessionId"],
            plan_id=raw["planId"],
            task_id=raw["taskId"],
            agent_id=raw["agentId"],
            job_idempotency_key=raw["jobIdempotencyKey"],
            running_task_revision=raw["runningTaskRevision"],
            terminal_task_revision=raw["terminalTaskRevision"],
            attempt_id=raw["attemptId"],
            attempt_number=raw["attemptNumber"],
            lease_epoch=raw["leaseEpoch"],
            worker_id=raw["workerId"],
            lease_token_digest=raw["leaseTokenDigest"],
            start_receipt_digest=raw["startReceiptDigest"],
            execution_manifest_digest=raw["executionManifestDigest"],
            result_manifest_schema_version=raw["resultManifestSchemaVersion"],
            result_manifest_digest=raw["resultManifestDigest"],
            result_ref=raw["resultRef"],
            effect_class=_effect_class(raw["effectClass"]),
            action_receipt_set_digest=raw["actionReceiptSetDigest"],
            acceptance_idempotency_key=raw["acceptanceIdempotencyKey"],
            request_digest=raw["requestDigest"],
            accepted_at=raw["acceptedAt"],
            artifact_count=raw["artifactCount"],
        )

    def canonical_bytes(self) -> bytes:
        snapshot = _result_evidence_snapshot(self)
        return _canonical_json_bytes(ScopedInvocationResultEvidenceV2.to_dict(snapshot))

    def canonical_digest(self) -> str:
        snapshot = _result_evidence_snapshot(self)
        return hashlib.sha256(
            SCOPED_INVOCATION_RESULT_EVIDENCE_DOMAIN.encode("utf-8")
            + _canonical_json_bytes(ScopedInvocationResultEvidenceV2.to_dict(snapshot))
        ).hexdigest()


def _result_evidence_snapshot(evidence: object) -> ScopedInvocationResultEvidenceV2:
    if type(evidence) is not ScopedInvocationResultEvidenceV2:
        raise TypeError("evidence must be exact ScopedInvocationResultEvidenceV2")
    return ScopedInvocationResultEvidenceV2.from_dict(
        ScopedInvocationResultEvidenceV2.to_dict(evidence)
    )


@dataclass(frozen=True)
class ScopedInvocationResultTerminalTransitionV2:
    """Capability-free scoped schema-2 intent for one result-bound terminal event."""

    schema_version: int
    transition_kind: str
    tenant_id: str
    workspace_id: str
    invocation_id: str
    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    job_idempotency_key: str
    runtime_revision: str
    correlation_id: str
    previous: TaskStatus
    current: TaskStatus
    reason: None
    running_task_revision: int
    terminal_task_revision: int
    result_receipt_id: str
    result_event_id: str
    result_evidence_digest: str

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationResultTerminalTransitionV2:
            raise TypeError(
                "terminal transition must be exact ScopedInvocationResultTerminalTransitionV2"
            )
        if type(self.schema_version) is not int:
            raise TypeError("schemaVersion must be an exact integer")
        if self.schema_version != SCOPED_INVOCATION_RESULT_TERMINAL_TRANSITION_SCHEMA_VERSION:
            raise ValueError("schemaVersion is unsupported")
        if type(self.transition_kind) is not str:
            raise TypeError("transitionKind must be a plain string")
        if self.transition_kind != "attempt_bound_result_accepted":
            raise ValueError("transitionKind must be attempt_bound_result_accepted")
        for label, value in (
            ("tenantId", self.tenant_id),
            ("workspaceId", self.workspace_id),
            ("invocationId", self.invocation_id),
            ("sessionId", self.session_id),
            ("planId", self.plan_id),
            ("taskId", self.task_id),
            ("agentId", self.agent_id),
            ("jobIdempotencyKey", self.job_idempotency_key),
            ("runtimeRevision", self.runtime_revision),
            ("correlationId", self.correlation_id),
            ("resultReceiptId", self.result_receipt_id),
            ("resultEventId", self.result_event_id),
        ):
            _text(value, label)
        if type(self.previous) is not TaskStatus or self.previous is not TaskStatus.RUNNING:
            raise ValueError("previous must be the exact RUNNING task status")
        if type(self.current) is not TaskStatus or self.current is not TaskStatus.COMPLETED:
            raise ValueError("current must be the exact COMPLETED task status")
        if self.reason is not None:
            raise ValueError("reason must be null for accepted result completion")
        running_revision = _positive_integer(
            self.running_task_revision,
            "runningTaskRevision",
        )
        terminal_revision = _positive_integer(
            self.terminal_task_revision,
            "terminalTaskRevision",
        )
        if running_revision >= _MAX_SQLITE_INTEGER or terminal_revision != running_revision + 1:
            raise ValueError("terminalTaskRevision must immediately follow runningTaskRevision")
        _digest(self.result_evidence_digest, "resultEvidenceDigest")
        _text("session:" + self.session_id, "terminal streamId")
        _text(
            f"task-status:{self.task_id}:{terminal_revision}",
            "terminal idempotencyKey",
        )

    @property
    def stream_id(self) -> str:
        ScopedInvocationResultTerminalTransitionV2.__post_init__(self)
        return "session:" + self.session_id

    @property
    def actor_id(self) -> str:
        ScopedInvocationResultTerminalTransitionV2.__post_init__(self)
        return CANONICAL_ORCHESTRATOR_ACTOR_ID

    @property
    def causation_id(self) -> str:
        ScopedInvocationResultTerminalTransitionV2.__post_init__(self)
        return self.result_event_id

    @property
    def idempotency_key(self) -> str:
        ScopedInvocationResultTerminalTransitionV2.__post_init__(self)
        return f"task-status:{self.task_id}:{self.terminal_task_revision}"

    def to_dict(self) -> Dict[str, object]:
        ScopedInvocationResultTerminalTransitionV2.__post_init__(self)
        return {
            "schemaVersion": self.schema_version,
            "transitionKind": self.transition_kind,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "invocationId": self.invocation_id,
            "sessionId": self.session_id,
            "planId": self.plan_id,
            "taskId": self.task_id,
            "agentId": self.agent_id,
            "jobIdempotencyKey": self.job_idempotency_key,
            "runtimeRevision": self.runtime_revision,
            "correlationId": self.correlation_id,
            "previous": self.previous.value,
            "current": self.current.value,
            "reason": self.reason,
            "runningTaskRevision": self.running_task_revision,
            "terminalTaskRevision": self.terminal_task_revision,
            "resultReceiptId": self.result_receipt_id,
            "resultEventId": self.result_event_id,
            "resultEvidenceDigest": self.result_evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> ScopedInvocationResultTerminalTransitionV2:
        if cls is not ScopedInvocationResultTerminalTransitionV2:
            raise TypeError("terminal transition decoder requires the exact schema-2 class")
        raw = _exact_dict(value, set(_TERMINAL_TRANSITION_FIELDS), "terminal transition")
        previous = raw["previous"]
        current = raw["current"]
        if type(previous) is not str or type(current) is not str:
            raise TypeError("terminal transition statuses must be plain strings")
        if previous != TaskStatus.RUNNING.value or current != TaskStatus.COMPLETED.value:
            raise ValueError("terminal transition must be exactly RUNNING to COMPLETED")
        return cls(
            schema_version=raw["schemaVersion"],
            transition_kind=raw["transitionKind"],
            tenant_id=raw["tenantId"],
            workspace_id=raw["workspaceId"],
            invocation_id=raw["invocationId"],
            session_id=raw["sessionId"],
            plan_id=raw["planId"],
            task_id=raw["taskId"],
            agent_id=raw["agentId"],
            job_idempotency_key=raw["jobIdempotencyKey"],
            runtime_revision=raw["runtimeRevision"],
            correlation_id=raw["correlationId"],
            previous=TaskStatus.RUNNING,
            current=TaskStatus.COMPLETED,
            reason=raw["reason"],
            running_task_revision=raw["runningTaskRevision"],
            terminal_task_revision=raw["terminalTaskRevision"],
            result_receipt_id=raw["resultReceiptId"],
            result_event_id=raw["resultEventId"],
            result_evidence_digest=raw["resultEvidenceDigest"],
        )

    def canonical_bytes(self) -> bytes:
        snapshot = _terminal_transition_snapshot(self)
        return _canonical_json_bytes(ScopedInvocationResultTerminalTransitionV2.to_dict(snapshot))

    def canonical_digest(self) -> str:
        snapshot = _terminal_transition_snapshot(self)
        return hashlib.sha256(
            SCOPED_INVOCATION_RESULT_TERMINAL_TRANSITION_DOMAIN.encode("utf-8")
            + _canonical_json_bytes(ScopedInvocationResultTerminalTransitionV2.to_dict(snapshot))
        ).hexdigest()


def _terminal_transition_snapshot(
    transition: object,
) -> ScopedInvocationResultTerminalTransitionV2:
    if type(transition) is not ScopedInvocationResultTerminalTransitionV2:
        raise TypeError("transition must be exact ScopedInvocationResultTerminalTransitionV2")
    return ScopedInvocationResultTerminalTransitionV2.from_dict(
        ScopedInvocationResultTerminalTransitionV2.to_dict(transition)
    )


def _result_start_receipt_snapshot(receipt: object) -> ScopedInvocationStartReceiptV3:
    if type(receipt) is not ScopedInvocationStartReceiptV3:
        raise TypeError("startReceipt must be exact ScopedInvocationStartReceiptV3")
    return ScopedInvocationStartReceiptV3.from_dict(ScopedInvocationStartReceiptV3.to_dict(receipt))


def _validate_result_receipt_graph(
    *,
    receipt_id: str,
    start_receipt: ScopedInvocationStartReceiptV3,
    evidence: ScopedInvocationResultEvidenceV2,
    result_event: ScopedInvocationResultEventCoordinatesV2,
    terminal_event: ScopedInvocationResultEventCoordinatesV2,
    terminal_transition: ScopedInvocationResultTerminalTransitionV2,
) -> None:
    start_evidence = start_receipt.evidence
    start_bindings = (
        (evidence.tenant_id, start_evidence.tenant_id, "tenantId"),
        (evidence.workspace_id, start_evidence.workspace_id, "workspaceId"),
        (evidence.invocation_id, start_evidence.invocation_id, "invocationId"),
        (evidence.session_id, start_evidence.session_id, "sessionId"),
        (evidence.plan_id, start_evidence.plan_id, "planId"),
        (evidence.task_id, start_evidence.task_id, "taskId"),
        (evidence.agent_id, start_evidence.agent_id, "agentId"),
        (
            evidence.job_idempotency_key,
            start_evidence.job_idempotency_key,
            "jobIdempotencyKey",
        ),
        (evidence.attempt_id, start_evidence.attempt_id, "attemptId"),
        (evidence.attempt_number, start_evidence.attempt_number, "attemptNumber"),
        (evidence.lease_epoch, start_evidence.lease_epoch, "leaseEpoch"),
        (evidence.worker_id, start_evidence.worker_id, "workerId"),
        (
            evidence.lease_token_digest,
            start_evidence.lease_token_digest,
            "leaseTokenDigest",
        ),
        (
            evidence.execution_manifest_digest,
            start_evidence.manifest_digest,
            "executionManifestDigest",
        ),
    )
    for actual, expected, label in start_bindings:
        if actual != expected:
            raise ValueError(f"result receipt evidence {label} does not match startReceipt")
    if evidence.start_receipt_digest != scoped_invocation_start_receipt_digest_v3(start_receipt):
        raise ValueError("result receipt evidence startReceiptDigest does not match startReceipt")
    if evidence.accepted_at < start_evidence.claimed_at:
        raise ValueError("result receipt acceptedAt must not precede the start claim")

    transition_bindings = (
        (terminal_transition.tenant_id, evidence.tenant_id, "tenantId"),
        (terminal_transition.workspace_id, evidence.workspace_id, "workspaceId"),
        (terminal_transition.invocation_id, evidence.invocation_id, "invocationId"),
        (terminal_transition.session_id, evidence.session_id, "sessionId"),
        (terminal_transition.plan_id, evidence.plan_id, "planId"),
        (terminal_transition.task_id, evidence.task_id, "taskId"),
        (terminal_transition.agent_id, evidence.agent_id, "agentId"),
        (
            terminal_transition.job_idempotency_key,
            evidence.job_idempotency_key,
            "jobIdempotencyKey",
        ),
        (
            terminal_transition.runtime_revision,
            start_evidence.runtime_revision,
            "runtimeRevision",
        ),
        (
            terminal_transition.correlation_id,
            start_evidence.correlation_id,
            "correlationId",
        ),
        (
            terminal_transition.running_task_revision,
            evidence.running_task_revision,
            "runningTaskRevision",
        ),
        (
            terminal_transition.terminal_task_revision,
            evidence.terminal_task_revision,
            "terminalTaskRevision",
        ),
        (
            terminal_transition.result_receipt_id,
            evidence.receipt_id,
            "resultReceiptId",
        ),
        (
            terminal_transition.result_evidence_digest,
            ScopedInvocationResultEvidenceV2.canonical_digest(evidence),
            "resultEvidenceDigest",
        ),
        (terminal_transition.result_event_id, result_event.event_id, "resultEventId"),
    )
    for actual, expected, label in transition_bindings:
        if actual != expected:
            raise ValueError(f"result receipt terminalTransition {label} is misbound")

    if receipt_id != evidence.receipt_id or receipt_id != terminal_transition.result_receipt_id:
        raise ValueError("result receipt receiptId is misbound")
    expected_stream_id = start_receipt.stream_id
    if expected_stream_id != "session:" + evidence.session_id:
        raise ValueError("result receipt start stream is misbound")
    if terminal_transition.stream_id != expected_stream_id:
        raise ValueError("result receipt terminalTransition stream is misbound")
    if result_event.stream_id != expected_stream_id:
        raise ValueError("result receipt result event stream is misbound")
    if terminal_event.stream_id != expected_stream_id:
        raise ValueError("result receipt terminal event stream is misbound")
    if result_event.event_type != TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE:
        raise ValueError("result receipt result event type is unsupported")
    if terminal_event.event_type != TASK_STATUS_CHANGED_EVENT_TYPE:
        raise ValueError("result receipt terminal event type is unsupported")
    if len({start_receipt.event_id, result_event.event_id, terminal_event.event_id}) != 3:
        raise ValueError("result receipt event IDs must be distinct")
    if result_event.sequence <= start_receipt.sequence:
        raise ValueError("result receipt result event must follow the start stream sequence")
    if result_event.global_position <= start_receipt.global_position:
        raise ValueError("result receipt result event must follow the start global position")
    if terminal_event.sequence != result_event.sequence + 1:
        raise ValueError("result receipt terminal stream sequence must follow the result event")
    if terminal_event.global_position != result_event.global_position + 1:
        raise ValueError("result receipt terminal global position must follow the result event")
    if terminal_event.event_envelope_digest == result_event.event_envelope_digest:
        raise ValueError("result receipt event envelope digests must be distinct")


def _result_receipt_identity_dict(
    *,
    schema_version: int,
    receipt_id: str,
    start_receipt: ScopedInvocationStartReceiptV3,
    evidence: ScopedInvocationResultEvidenceV2,
    result_event: ScopedInvocationResultEventCoordinatesV2,
    terminal_event: ScopedInvocationResultEventCoordinatesV2,
    terminal_transition: ScopedInvocationResultTerminalTransitionV2,
) -> Dict[str, object]:
    return {
        "schemaVersion": schema_version,
        "receiptId": receipt_id,
        "startReceipt": ScopedInvocationStartReceiptV3.to_dict(start_receipt),
        "evidence": ScopedInvocationResultEvidenceV2.to_dict(evidence),
        "resultEvent": ScopedInvocationResultEventCoordinatesV2.to_dict(result_event),
        "terminalEvent": ScopedInvocationResultEventCoordinatesV2.to_dict(terminal_event),
        "terminalTransition": ScopedInvocationResultTerminalTransitionV2.to_dict(
            terminal_transition
        ),
    }


def _result_receipt_digest_from_parts(
    *,
    schema_version: int,
    receipt_id: str,
    start_receipt: ScopedInvocationStartReceiptV3,
    evidence: ScopedInvocationResultEvidenceV2,
    result_event: ScopedInvocationResultEventCoordinatesV2,
    terminal_event: ScopedInvocationResultEventCoordinatesV2,
    terminal_transition: ScopedInvocationResultTerminalTransitionV2,
) -> str:
    return hashlib.sha256(
        SCOPED_INVOCATION_RESULT_RECEIPT_DOMAIN.encode("utf-8")
        + _canonical_json_bytes(
            _result_receipt_identity_dict(
                schema_version=schema_version,
                receipt_id=receipt_id,
                start_receipt=start_receipt,
                evidence=evidence,
                result_event=result_event,
                terminal_event=terminal_event,
                terminal_transition=terminal_transition,
            )
        )
    ).hexdigest()


@dataclass(frozen=True)
class ScopedInvocationResultReceiptV2:
    """Capability-free, self-verifying value for one store-shaped result event graph.

    Codec validity alone grants no authority.  A store must reconstruct and verify the
    complete persisted request, Artifact, event, job, attempt, and outbox graph before it
    may return this value as an accepted or observed outcome.
    """

    schema_version: int
    receipt_id: str
    start_receipt: ScopedInvocationStartReceiptV3 = field(repr=False)
    evidence: ScopedInvocationResultEvidenceV2 = field(repr=False)
    result_event: ScopedInvocationResultEventCoordinatesV2
    terminal_event: ScopedInvocationResultEventCoordinatesV2
    terminal_transition: ScopedInvocationResultTerminalTransitionV2 = field(repr=False)
    receipt_digest: str

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationResultReceiptV2:
            raise TypeError("result receipt must be exact ScopedInvocationResultReceiptV2")
        if type(self.schema_version) is not int:
            raise TypeError("schemaVersion must be an exact integer")
        if self.schema_version != SCOPED_INVOCATION_RESULT_RECEIPT_SCHEMA_VERSION:
            raise ValueError("schemaVersion is unsupported")
        receipt_id = _text(self.receipt_id, "receiptId")
        start_receipt = _result_start_receipt_snapshot(self.start_receipt)
        evidence = _result_evidence_snapshot(self.evidence)
        result_event = _result_event_coordinates_snapshot(self.result_event)
        terminal_event = _result_event_coordinates_snapshot(self.terminal_event)
        terminal_transition = _terminal_transition_snapshot(self.terminal_transition)
        receipt_digest = _digest(self.receipt_digest, "receiptDigest")
        _validate_result_receipt_graph(
            receipt_id=receipt_id,
            start_receipt=start_receipt,
            evidence=evidence,
            result_event=result_event,
            terminal_event=terminal_event,
            terminal_transition=terminal_transition,
        )
        expected_digest = _result_receipt_digest_from_parts(
            schema_version=self.schema_version,
            receipt_id=receipt_id,
            start_receipt=start_receipt,
            evidence=evidence,
            result_event=result_event,
            terminal_event=terminal_event,
            terminal_transition=terminal_transition,
        )
        if not hmac.compare_digest(receipt_digest, expected_digest):
            raise ValueError("receiptDigest does not match the canonical result receipt graph")
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "start_receipt", start_receipt)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "result_event", result_event)
        object.__setattr__(self, "terminal_event", terminal_event)
        object.__setattr__(self, "terminal_transition", terminal_transition)
        object.__setattr__(self, "receipt_digest", receipt_digest)

    def to_dict(self) -> Dict[str, object]:
        ScopedInvocationResultReceiptV2.__post_init__(self)
        value = _result_receipt_identity_dict(
            schema_version=self.schema_version,
            receipt_id=self.receipt_id,
            start_receipt=self.start_receipt,
            evidence=self.evidence,
            result_event=self.result_event,
            terminal_event=self.terminal_event,
            terminal_transition=self.terminal_transition,
        )
        value["receiptDigest"] = self.receipt_digest
        return value

    @classmethod
    def from_dict(cls, value: object) -> ScopedInvocationResultReceiptV2:
        if cls is not ScopedInvocationResultReceiptV2:
            raise TypeError("result receipt decoder requires the exact schema-2 class")
        raw = _exact_dict(value, set(_RESULT_RECEIPT_FIELDS), "scoped result receipt")
        return cls(
            schema_version=raw["schemaVersion"],
            receipt_id=raw["receiptId"],
            start_receipt=ScopedInvocationStartReceiptV3.from_dict(raw["startReceipt"]),
            evidence=ScopedInvocationResultEvidenceV2.from_dict(raw["evidence"]),
            result_event=ScopedInvocationResultEventCoordinatesV2.from_dict(raw["resultEvent"]),
            terminal_event=ScopedInvocationResultEventCoordinatesV2.from_dict(raw["terminalEvent"]),
            terminal_transition=ScopedInvocationResultTerminalTransitionV2.from_dict(
                raw["terminalTransition"]
            ),
            receipt_digest=raw["receiptDigest"],
        )

    def canonical_bytes(self) -> bytes:
        """Return the receipt-digest body; the self digest is intentionally excluded."""

        snapshot = _result_receipt_snapshot(self)
        return _canonical_json_bytes(
            _result_receipt_identity_dict(
                schema_version=snapshot.schema_version,
                receipt_id=snapshot.receipt_id,
                start_receipt=snapshot.start_receipt,
                evidence=snapshot.evidence,
                result_event=snapshot.result_event,
                terminal_event=snapshot.terminal_event,
                terminal_transition=snapshot.terminal_transition,
            )
        )

    def canonical_digest(self) -> str:
        snapshot = _result_receipt_snapshot(self)
        return snapshot.receipt_digest


def _result_receipt_snapshot(receipt: object) -> ScopedInvocationResultReceiptV2:
    if type(receipt) is not ScopedInvocationResultReceiptV2:
        raise TypeError("receipt must be exact ScopedInvocationResultReceiptV2")
    return ScopedInvocationResultReceiptV2.from_dict(
        ScopedInvocationResultReceiptV2.to_dict(receipt)
    )


@dataclass(frozen=True)
class ScopedInvocationResultManifestV2:
    """Canonical schema-2 proposal for one scoped invocation result."""

    schema_version: int
    tenant_id: str
    workspace_id: str
    invocation_id: str
    session_id: str
    plan_id: str
    task_id: str
    agent_id: str
    job_idempotency_key: str
    task_revision: int
    correlation_id: str
    causation_id: str
    runtime_revision: str
    execution_manifest_digest: str
    effect_class: EffectClass
    action_receipt_set_digest: str
    result_ref: str
    narration: str = field(repr=False)
    metadata: Mapping[str, object] = field(repr=False)
    primary_artifact_id: str | None
    artifacts: Tuple[ScopedInvocationResultArtifactV2, ...] = field(repr=False)
    _metadata_canonical_bytes: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationResultManifestV2:
            raise TypeError("result manifest must be an exact ScopedInvocationResultManifestV2")
        if type(self.schema_version) is not int:
            raise TypeError("schemaVersion must be an exact integer")
        if self.schema_version != SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION:
            raise ValueError("schemaVersion is unsupported")
        for label, value in (
            ("tenantId", self.tenant_id),
            ("workspaceId", self.workspace_id),
            ("invocationId", self.invocation_id),
            ("sessionId", self.session_id),
            ("planId", self.plan_id),
            ("taskId", self.task_id),
            ("agentId", self.agent_id),
            ("jobIdempotencyKey", self.job_idempotency_key),
            ("correlationId", self.correlation_id),
            ("causationId", self.causation_id),
            ("runtimeRevision", self.runtime_revision),
            ("resultRef", self.result_ref),
        ):
            _text(value, label)
        _positive_integer(self.task_revision, "taskRevision")
        _digest(self.execution_manifest_digest, "executionManifestDigest")
        _digest(self.action_receipt_set_digest, "actionReceiptSetDigest")
        _body_text(self.narration, "narration", maximum_bytes=_MAX_NARRATION_BYTES)
        metadata, metadata_bytes = _snapshot_json_object(
            self.metadata,
            "metadata",
            allow_frozen=True,
        )
        if self.primary_artifact_id is not None:
            _text(self.primary_artifact_id, "primaryArtifactId")
        if self.causation_id != self.task_id:
            raise ValueError("causationId must equal taskId")
        if type(self.effect_class) is not EffectClass:
            raise TypeError("effect_class must be an exact EffectClass")
        if self.effect_class is EffectClass.PURE:
            if self.action_receipt_set_digest != EMPTY_ACTION_RECEIPT_SET_DIGEST:
                raise ValueError("pure results require the canonical empty action receipt set")
        elif self.action_receipt_set_digest == EMPTY_ACTION_RECEIPT_SET_DIGEST:
            raise ValueError("effectful results require a non-empty action receipt set digest")
        if type(self.artifacts) is not tuple:
            raise TypeError("artifacts must be an exact tuple")
        if len(self.artifacts) > _MAX_ARTIFACTS:
            raise ValueError("artifacts must contain at most 256 descriptors")

        artifacts: list[ScopedInvocationResultArtifactV2] = []
        artifact_ids: set[str] = set()
        artifact_names: set[str] = set()
        idempotency_keys: set[str] = set()
        for item in self.artifacts:
            if type(item) is not ScopedInvocationResultArtifactV2:
                raise TypeError("artifacts require exact ScopedInvocationResultArtifactV2 values")
            snapshot = ScopedInvocationResultArtifactV2.from_dict(item.to_dict())
            if snapshot.created_by != self.agent_id:
                raise ValueError("artifact createdBy must equal the result agentId")
            if snapshot.artifact_id in artifact_ids:
                raise ValueError("result manifest repeats an artifactId")
            if snapshot.name in artifact_names:
                raise ValueError("result manifest repeats an artifact name")
            if snapshot.idempotency_key in idempotency_keys:
                raise ValueError("result manifest repeats an artifact idempotencyKey")
            artifact_ids.add(snapshot.artifact_id)
            artifact_names.add(snapshot.name)
            idempotency_keys.add(snapshot.idempotency_key)
            artifacts.append(snapshot)
        if self.primary_artifact_id is not None and self.primary_artifact_id not in artifact_ids:
            raise ValueError("primaryArtifactId must identify one descriptor in artifacts")
        if self.result_ref in artifact_ids:
            raise ValueError("resultRef must not alias an artifactId")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "_metadata_canonical_bytes", metadata_bytes)
        object.__setattr__(self, "artifacts", tuple(artifacts))
        if len(_canonical_json_bytes(self._wire_dict())) > _MAX_MANIFEST_BYTES:
            raise ValueError("result manifest exceeds its canonical byte limit")

    def _wire_dict(self) -> Dict[str, object]:
        metadata = _thaw_json(self.metadata)
        if type(metadata) is not dict:  # pragma: no cover - protected by construction.
            raise TypeError("metadata snapshot is not a JSON object")
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "workspaceId": self.workspace_id,
            "invocationId": self.invocation_id,
            "sessionId": self.session_id,
            "planId": self.plan_id,
            "taskId": self.task_id,
            "agentId": self.agent_id,
            "jobIdempotencyKey": self.job_idempotency_key,
            "taskRevision": self.task_revision,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "runtimeRevision": self.runtime_revision,
            "executionManifestDigest": self.execution_manifest_digest,
            "effectClass": self.effect_class.value,
            "actionReceiptSetDigest": self.action_receipt_set_digest,
            "resultRef": self.result_ref,
            "narration": self.narration,
            "metadata": metadata,
            "primaryArtifactId": self.primary_artifact_id,
            "artifacts": [item.to_dict() for item in self.artifacts],
        }

    def to_dict(self) -> Dict[str, object]:
        ScopedInvocationResultManifestV2.__post_init__(self)
        return self._wire_dict()

    @classmethod
    def from_dict(cls, value: object) -> ScopedInvocationResultManifestV2:
        if cls is not ScopedInvocationResultManifestV2:
            raise TypeError("result manifest decoder requires the exact schema-2 class")
        raw = _exact_dict(value, set(_MANIFEST_FIELDS), "scoped invocation result manifest")
        raw_artifacts = raw["artifacts"]
        if type(raw_artifacts) is not list:
            raise TypeError("artifacts wire value must be a plain list")
        return cls(
            schema_version=raw["schemaVersion"],
            tenant_id=raw["tenantId"],
            workspace_id=raw["workspaceId"],
            invocation_id=raw["invocationId"],
            session_id=raw["sessionId"],
            plan_id=raw["planId"],
            task_id=raw["taskId"],
            agent_id=raw["agentId"],
            job_idempotency_key=raw["jobIdempotencyKey"],
            task_revision=raw["taskRevision"],
            correlation_id=raw["correlationId"],
            causation_id=raw["causationId"],
            runtime_revision=raw["runtimeRevision"],
            execution_manifest_digest=raw["executionManifestDigest"],
            effect_class=_effect_class(raw["effectClass"]),
            action_receipt_set_digest=raw["actionReceiptSetDigest"],
            result_ref=raw["resultRef"],
            narration=raw["narration"],
            metadata=raw["metadata"],
            primary_artifact_id=raw["primaryArtifactId"],
            artifacts=tuple(
                ScopedInvocationResultArtifactV2.from_dict(item) for item in raw_artifacts
            ),
        )

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ValueError("result manifest exceeds its canonical byte limit")
        return encoded

    def canonical_digest(self) -> str:
        return hashlib.sha256(
            SCOPED_INVOCATION_RESULT_MANIFEST_DOMAIN.encode("utf-8") + self.canonical_bytes()
        ).hexdigest()


def scoped_invocation_start_receipt_digest_v3(receipt: object) -> str:
    if type(receipt) is not ScopedInvocationStartReceiptV3:
        raise TypeError("start receipt digest requires exact ScopedInvocationStartReceiptV3")
    snapshot = ScopedInvocationStartReceiptV3.from_dict(
        ScopedInvocationStartReceiptV3.to_dict(receipt)
    )
    return hashlib.sha256(
        SCOPED_INVOCATION_START_RECEIPT_DIGEST_DOMAIN.encode("utf-8")
        + _canonical_json_bytes(ScopedInvocationStartReceiptV3.to_dict(snapshot))
    ).hexdigest()


def _artifact_candidate_snapshot(
    candidate: object,
) -> ScopedInvocationResultArtifactCandidateV2:
    if type(candidate) is not ScopedInvocationResultArtifactCandidateV2:
        raise TypeError(
            "artifact candidates require exact ScopedInvocationResultArtifactCandidateV2 values"
        )
    return ScopedInvocationResultArtifactCandidateV2(
        tenant_id=candidate.tenant_id,
        workspace_id=candidate.workspace_id,
        session_id=candidate.session_id,
        task_id=candidate.task_id,
        artifact_id=candidate.artifact_id,
        name=candidate.name,
        media_type=candidate.media_type,
        content=candidate.content,
        metadata_canonical_bytes=candidate.metadata_canonical_bytes,
        created_by=candidate.created_by,
        idempotency_key=candidate.idempotency_key,
        expected_head_version=candidate.expected_head_version,
    )


def _artifact_descriptor_snapshot(
    descriptor: object,
) -> ScopedInvocationResultArtifactV2:
    if type(descriptor) is not ScopedInvocationResultArtifactV2:
        raise TypeError(
            "artifact descriptors require exact ScopedInvocationResultArtifactV2 values"
        )
    return ScopedInvocationResultArtifactV2(
        artifact_id=descriptor.artifact_id,
        name=descriptor.name,
        version=descriptor.version,
        parent_version=descriptor.parent_version,
        media_type=descriptor.media_type,
        blob_digest=descriptor.blob_digest,
        byte_size=descriptor.byte_size,
        metadata_digest=descriptor.metadata_digest,
        created_by=descriptor.created_by,
        idempotency_key=descriptor.idempotency_key,
        request_digest=descriptor.request_digest,
    )


def _result_manifest_snapshot(manifest: object) -> ScopedInvocationResultManifestV2:
    if type(manifest) is not ScopedInvocationResultManifestV2:
        raise TypeError("manifest must be exact ScopedInvocationResultManifestV2")
    if type(manifest.artifacts) is not tuple:
        raise TypeError("artifacts must be an exact tuple")
    if len(manifest.artifacts) > _MAX_ARTIFACTS:
        raise ValueError("result manifest artifacts must contain at most 256 descriptors")
    artifacts = tuple(_artifact_descriptor_snapshot(item) for item in manifest.artifacts)
    return ScopedInvocationResultManifestV2(
        schema_version=manifest.schema_version,
        tenant_id=manifest.tenant_id,
        workspace_id=manifest.workspace_id,
        invocation_id=manifest.invocation_id,
        session_id=manifest.session_id,
        plan_id=manifest.plan_id,
        task_id=manifest.task_id,
        agent_id=manifest.agent_id,
        job_idempotency_key=manifest.job_idempotency_key,
        task_revision=manifest.task_revision,
        correlation_id=manifest.correlation_id,
        causation_id=manifest.causation_id,
        runtime_revision=manifest.runtime_revision,
        execution_manifest_digest=manifest.execution_manifest_digest,
        effect_class=manifest.effect_class,
        action_receipt_set_digest=manifest.action_receipt_set_digest,
        result_ref=manifest.result_ref,
        narration=manifest.narration,
        metadata=manifest.metadata,
        primary_artifact_id=manifest.primary_artifact_id,
        artifacts=artifacts,
    )


@dataclass(frozen=True)
class ScopedInvocationResultAcceptanceRequestV2:
    """Capability-free exact command body for future atomic result acceptance."""

    schema_version: int
    acceptance_idempotency_key: str
    start_receipt: ScopedInvocationStartReceiptV3 = field(repr=False)
    manifest: ScopedInvocationResultManifestV2 = field(repr=False)
    artifact_candidates: Tuple[ScopedInvocationResultArtifactCandidateV2, ...] = field(repr=False)
    expected_stream_version: int

    def __post_init__(self) -> None:
        if type(self) is not ScopedInvocationResultAcceptanceRequestV2:
            raise TypeError(
                "acceptance request must be exact ScopedInvocationResultAcceptanceRequestV2"
            )
        if type(self.schema_version) is not int:
            raise TypeError("schemaVersion must be an exact integer")
        if self.schema_version != SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_SCHEMA_VERSION:
            raise ValueError("schemaVersion is unsupported")
        _text(self.acceptance_idempotency_key, "acceptanceIdempotencyKey")
        if type(self.start_receipt) is not ScopedInvocationStartReceiptV3:
            raise TypeError("startReceipt must be exact ScopedInvocationStartReceiptV3")
        start_receipt = ScopedInvocationStartReceiptV3.from_dict(
            ScopedInvocationStartReceiptV3.to_dict(self.start_receipt)
        )
        manifest = _result_manifest_snapshot(self.manifest)
        if type(self.artifact_candidates) is not tuple:
            raise TypeError("artifactCandidates must be an exact tuple")
        if len(self.artifact_candidates) > _MAX_ARTIFACTS:
            raise ValueError("artifactCandidates must contain at most 256 values")
        if len(self.artifact_candidates) != len(manifest.artifacts):
            raise ValueError("artifact candidates and manifest descriptors have different counts")
        candidate_snapshots: list[ScopedInvocationResultArtifactCandidateV2] = []
        total_content_bytes = 0
        total_metadata_bytes = 0
        for item in self.artifact_candidates:
            candidate = _artifact_candidate_snapshot(item)
            for label, value in (
                ("tenantId", candidate.tenant_id),
                ("workspaceId", candidate.workspace_id),
                ("sessionId", candidate.session_id),
                ("taskId", candidate.task_id),
                ("artifactId", candidate.artifact_id),
                ("name", candidate.name),
                ("createdBy", candidate.created_by),
                ("idempotencyKey", candidate.idempotency_key),
            ):
                if len(value) > MAX_ARTIFACT_IDENTITY_CHARACTERS:
                    raise ValueError(
                        f"artifact candidate {label} exceeds the Artifact persistence "
                        f"limit of {MAX_ARTIFACT_IDENTITY_CHARACTERS} characters"
                    )
            total_content_bytes += candidate.byte_size
            total_metadata_bytes += len(candidate.metadata_canonical_bytes)
            if total_content_bytes > _MAX_RESULT_CONTENT_BYTES:
                raise ValueError("artifact candidates exceed the aggregate content byte limit")
            if total_metadata_bytes > _MAX_RESULT_ARTIFACT_METADATA_BYTES:
                raise ValueError("artifact candidates exceed the aggregate metadata byte limit")
            candidate_snapshots.append(candidate)
        candidates = tuple(candidate_snapshots)
        expected_stream_version = _nonnegative_integer(
            self.expected_stream_version,
            "expectedStreamVersion",
        )
        if expected_stream_version < start_receipt.sequence:
            raise ValueError("expectedStreamVersion must include the stored start event")
        if expected_stream_version > _MAX_SQLITE_INTEGER - 2:
            raise ValueError("expectedStreamVersion must leave space for two terminal events")
        if manifest.task_revision >= _MAX_SQLITE_INTEGER:
            raise ValueError("result manifest taskRevision cannot allocate a terminal revision")
        if manifest.effect_class is not EffectClass.PURE:
            raise ValueError("result acceptance admits only effectClass pure")
        if manifest.action_receipt_set_digest != EMPTY_ACTION_RECEIPT_SET_DIGEST:
            raise ValueError("result acceptance requires the empty action receipt set")

        evidence = start_receipt.evidence
        bindings = (
            (manifest.tenant_id, evidence.tenant_id, "tenantId"),
            (manifest.workspace_id, evidence.workspace_id, "workspaceId"),
            (manifest.invocation_id, evidence.invocation_id, "invocationId"),
            (manifest.session_id, evidence.session_id, "sessionId"),
            (manifest.plan_id, evidence.plan_id, "planId"),
            (manifest.task_id, evidence.task_id, "taskId"),
            (manifest.agent_id, evidence.agent_id, "agentId"),
            (
                manifest.job_idempotency_key,
                evidence.job_idempotency_key,
                "jobIdempotencyKey",
            ),
            (
                manifest.execution_manifest_digest,
                evidence.manifest_digest,
                "executionManifestDigest",
            ),
            (manifest.runtime_revision, evidence.runtime_revision, "runtimeRevision"),
            (manifest.correlation_id, evidence.correlation_id, "correlationId"),
            (manifest.causation_id, evidence.causation_id, "causationId"),
        )
        for actual, expected, label in bindings:
            if actual != expected:
                raise ValueError(f"result manifest {label} does not match the start receipt")

        for ordinal, (candidate, descriptor) in enumerate(zip(candidates, manifest.artifacts)):
            candidate_bindings = (
                (candidate.tenant_id, manifest.tenant_id, "tenantId"),
                (candidate.workspace_id, manifest.workspace_id, "workspaceId"),
                (candidate.session_id, manifest.session_id, "sessionId"),
                (candidate.task_id, manifest.task_id, "taskId"),
                (candidate.created_by, manifest.agent_id, "createdBy"),
            )
            for actual, expected, label in candidate_bindings:
                if actual != expected:
                    raise ValueError(
                        f"artifact candidate {ordinal} {label} does not match the result manifest"
                    )
            if candidate.to_descriptor() != descriptor:
                raise ValueError(
                    f"artifact candidate {ordinal} does not match its ordered descriptor"
                )
        object.__setattr__(self, "start_receipt", start_receipt)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "artifact_candidates", candidates)

    @property
    def start_receipt_digest(self) -> str:
        snapshot = _acceptance_request_snapshot(self)
        return scoped_invocation_start_receipt_digest_v3(snapshot.start_receipt)

    def _identity_dict(self) -> Dict[str, object]:
        return _acceptance_request_identity_dict(_acceptance_request_snapshot(self))

    def canonical_digest(self) -> str:
        snapshot = _acceptance_request_snapshot(self)
        return hashlib.sha256(
            SCOPED_INVOCATION_RESULT_ACCEPTANCE_REQUEST_DOMAIN.encode("utf-8")
            + _canonical_json_bytes(_acceptance_request_identity_dict(snapshot))
        ).hexdigest()


def _acceptance_request_snapshot(
    request: object,
) -> ScopedInvocationResultAcceptanceRequestV2:
    if type(request) is not ScopedInvocationResultAcceptanceRequestV2:
        raise TypeError("request must be exact ScopedInvocationResultAcceptanceRequestV2")
    return ScopedInvocationResultAcceptanceRequestV2(
        schema_version=request.schema_version,
        acceptance_idempotency_key=request.acceptance_idempotency_key,
        start_receipt=request.start_receipt,
        manifest=request.manifest,
        artifact_candidates=request.artifact_candidates,
        expected_stream_version=request.expected_stream_version,
    )


def _acceptance_request_identity_dict(
    request: ScopedInvocationResultAcceptanceRequestV2,
) -> Dict[str, object]:
    if type(request) is not ScopedInvocationResultAcceptanceRequestV2:
        raise TypeError("request identity requires exact ScopedInvocationResultAcceptanceRequestV2")
    return {
        "schemaVersion": request.schema_version,
        "acceptanceIdempotencyKey": request.acceptance_idempotency_key,
        "startReceiptDigest": scoped_invocation_start_receipt_digest_v3(request.start_receipt),
        "resultManifestDigest": ScopedInvocationResultManifestV2.canonical_digest(request.manifest),
        "artifactCandidateDigests": [
            ScopedInvocationResultArtifactCandidateV2.canonical_digest(candidate)
            for candidate in request.artifact_candidates
        ],
        "expectedStreamVersion": request.expected_stream_version,
    }


def build_scoped_invocation_result_terminal_transition_v2(
    request: object,
    evidence: object,
    *,
    result_event_id: object,
) -> ScopedInvocationResultTerminalTransitionV2:
    """Build one exact result-bound terminal payload without granting store authority."""

    request_snapshot = _acceptance_request_snapshot(request)
    evidence_snapshot = _result_evidence_snapshot(evidence)
    event_id = _text(result_event_id, "resultEventId")
    if event_id == request_snapshot.start_receipt.event_id:
        raise ValueError("resultEventId must differ from the invocation-start eventId")

    manifest = request_snapshot.manifest
    start_evidence = request_snapshot.start_receipt.evidence
    request_digest = ScopedInvocationResultAcceptanceRequestV2.canonical_digest(request_snapshot)
    manifest_digest = ScopedInvocationResultManifestV2.canonical_digest(manifest)
    start_receipt_digest = scoped_invocation_start_receipt_digest_v3(request_snapshot.start_receipt)
    bindings = (
        (evidence_snapshot.tenant_id, manifest.tenant_id, "tenantId"),
        (evidence_snapshot.workspace_id, manifest.workspace_id, "workspaceId"),
        (evidence_snapshot.invocation_id, manifest.invocation_id, "invocationId"),
        (evidence_snapshot.session_id, manifest.session_id, "sessionId"),
        (evidence_snapshot.plan_id, manifest.plan_id, "planId"),
        (evidence_snapshot.task_id, manifest.task_id, "taskId"),
        (evidence_snapshot.agent_id, manifest.agent_id, "agentId"),
        (
            evidence_snapshot.job_idempotency_key,
            manifest.job_idempotency_key,
            "jobIdempotencyKey",
        ),
        (evidence_snapshot.attempt_id, start_evidence.attempt_id, "attemptId"),
        (
            evidence_snapshot.attempt_number,
            start_evidence.attempt_number,
            "attemptNumber",
        ),
        (evidence_snapshot.lease_epoch, start_evidence.lease_epoch, "leaseEpoch"),
        (evidence_snapshot.worker_id, start_evidence.worker_id, "workerId"),
        (
            evidence_snapshot.lease_token_digest,
            start_evidence.lease_token_digest,
            "leaseTokenDigest",
        ),
        (
            evidence_snapshot.start_receipt_digest,
            start_receipt_digest,
            "startReceiptDigest",
        ),
        (
            evidence_snapshot.execution_manifest_digest,
            manifest.execution_manifest_digest,
            "executionManifestDigest",
        ),
        (
            evidence_snapshot.result_manifest_schema_version,
            manifest.schema_version,
            "resultManifestSchemaVersion",
        ),
        (
            evidence_snapshot.result_manifest_digest,
            manifest_digest,
            "resultManifestDigest",
        ),
        (evidence_snapshot.result_ref, manifest.result_ref, "resultRef"),
        (evidence_snapshot.effect_class, manifest.effect_class, "effectClass"),
        (
            evidence_snapshot.action_receipt_set_digest,
            manifest.action_receipt_set_digest,
            "actionReceiptSetDigest",
        ),
        (
            evidence_snapshot.acceptance_idempotency_key,
            request_snapshot.acceptance_idempotency_key,
            "acceptanceIdempotencyKey",
        ),
        (evidence_snapshot.request_digest, request_digest, "requestDigest"),
        (
            evidence_snapshot.artifact_count,
            len(request_snapshot.artifact_candidates),
            "artifactCount",
        ),
        (
            evidence_snapshot.running_task_revision,
            manifest.task_revision,
            "runningTaskRevision",
        ),
        (
            evidence_snapshot.terminal_task_revision,
            manifest.task_revision + 1,
            "terminalTaskRevision",
        ),
    )
    for actual, expected, label in bindings:
        if actual != expected:
            raise ValueError(f"result evidence {label} does not match the acceptance request")

    return ScopedInvocationResultTerminalTransitionV2(
        schema_version=SCOPED_INVOCATION_RESULT_TERMINAL_TRANSITION_SCHEMA_VERSION,
        transition_kind="attempt_bound_result_accepted",
        tenant_id=manifest.tenant_id,
        workspace_id=manifest.workspace_id,
        invocation_id=manifest.invocation_id,
        session_id=manifest.session_id,
        plan_id=manifest.plan_id,
        task_id=manifest.task_id,
        agent_id=manifest.agent_id,
        job_idempotency_key=manifest.job_idempotency_key,
        runtime_revision=manifest.runtime_revision,
        correlation_id=manifest.correlation_id,
        previous=TaskStatus.RUNNING,
        current=TaskStatus.COMPLETED,
        reason=None,
        running_task_revision=evidence_snapshot.running_task_revision,
        terminal_task_revision=evidence_snapshot.terminal_task_revision,
        result_receipt_id=evidence_snapshot.receipt_id,
        result_event_id=event_id,
        result_evidence_digest=ScopedInvocationResultEvidenceV2.canonical_digest(evidence_snapshot),
    )


def build_scoped_invocation_result_receipt_v2(
    request: object,
    evidence: object,
    *,
    result_event: object,
    terminal_event: object,
    terminal_transition: object,
) -> ScopedInvocationResultReceiptV2:
    """Build a capability-free receipt value from one exact store-shaped event graph.

    The caller must still be a store boundary that validates these coordinates and opaque
    envelope digests against durable rows in one consistent snapshot.  This pure builder
    intentionally does not grant fresh-commit or replay authority.
    """

    request_snapshot = _acceptance_request_snapshot(request)
    evidence_snapshot = _result_evidence_snapshot(evidence)
    result_coordinates = _result_event_coordinates_snapshot(result_event)
    terminal_coordinates = _result_event_coordinates_snapshot(terminal_event)
    transition_snapshot = _terminal_transition_snapshot(terminal_transition)
    expected_transition = build_scoped_invocation_result_terminal_transition_v2(
        request_snapshot,
        evidence_snapshot,
        result_event_id=result_coordinates.event_id,
    )
    if transition_snapshot != expected_transition:
        raise ValueError("terminalTransition does not match the exact acceptance request")
    if result_coordinates.sequence != request_snapshot.expected_stream_version + 1:
        raise ValueError("result event sequence does not follow expectedStreamVersion")
    if terminal_coordinates.sequence != request_snapshot.expected_stream_version + 2:
        raise ValueError("terminal event sequence does not follow expectedStreamVersion")

    receipt_id = evidence_snapshot.receipt_id
    start_receipt = _result_start_receipt_snapshot(request_snapshot.start_receipt)
    _validate_result_receipt_graph(
        receipt_id=receipt_id,
        start_receipt=start_receipt,
        evidence=evidence_snapshot,
        result_event=result_coordinates,
        terminal_event=terminal_coordinates,
        terminal_transition=transition_snapshot,
    )
    receipt_digest = _result_receipt_digest_from_parts(
        schema_version=SCOPED_INVOCATION_RESULT_RECEIPT_SCHEMA_VERSION,
        receipt_id=receipt_id,
        start_receipt=start_receipt,
        evidence=evidence_snapshot,
        result_event=result_coordinates,
        terminal_event=terminal_coordinates,
        terminal_transition=transition_snapshot,
    )
    return ScopedInvocationResultReceiptV2(
        schema_version=SCOPED_INVOCATION_RESULT_RECEIPT_SCHEMA_VERSION,
        receipt_id=receipt_id,
        start_receipt=start_receipt,
        evidence=evidence_snapshot,
        result_event=result_coordinates,
        terminal_event=terminal_coordinates,
        terminal_transition=transition_snapshot,
        receipt_digest=receipt_digest,
    )


__all__ = [
    "EMPTY_ACTION_RECEIPT_SET_DIGEST",
    "SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION",
    "ScopedInvocationResultArtifactV2",
    "ScopedInvocationResultManifestV2",
]
