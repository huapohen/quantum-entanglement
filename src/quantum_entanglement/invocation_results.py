# ruff: noqa: UP006, UP035
"""Strict, side-effect-free codecs for scoped invocation results.

These immutable values describe a proposed result and its Artifact identities.  They are
not durable receipts, do not authorize completion, and never carry a plaintext lease.  A
future EventStore acceptor must revalidate them against one exact scoped start claim and
commit the complete receipt graph before any result becomes authoritative.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Set, Tuple, cast

from .invocation_execution import EffectClass

SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION = 2
SCOPED_INVOCATION_RESULT_MANIFEST_DOMAIN = "quantum-entanglement.invocation-result-manifest/2\n"
ACTION_RECEIPT_SET_DOMAIN = "quantum-entanglement.action-receipt-set/1\n"
EMPTY_ACTION_RECEIPT_SET_DIGEST = hashlib.sha256(
    ACTION_RECEIPT_SET_DOMAIN.encode("utf-8") + b"[]"
).hexdigest()

_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_IDENTITY_BYTES = 512
_MAX_MEDIA_TYPE_BYTES = 255
_MAX_ARTIFACTS = 256
_MAX_MANIFEST_BYTES = 1_048_576
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_BLOB_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")

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
        "artifacts",
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
    artifacts: Tuple[ScopedInvocationResultArtifactV2, ...] = field(repr=False)

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
        if not self.artifacts or len(self.artifacts) > _MAX_ARTIFACTS:
            raise ValueError("artifacts must contain between 1 and 256 descriptors")

        artifacts: list[ScopedInvocationResultArtifactV2] = []
        artifact_ids: set[str] = set()
        version_heads: set[tuple[str, int]] = set()
        idempotency_keys: set[str] = set()
        for item in self.artifacts:
            if type(item) is not ScopedInvocationResultArtifactV2:
                raise TypeError("artifacts require exact ScopedInvocationResultArtifactV2 values")
            snapshot = ScopedInvocationResultArtifactV2.from_dict(item.to_dict())
            if snapshot.created_by != self.agent_id:
                raise ValueError("artifact createdBy must equal the result agentId")
            if snapshot.artifact_id in artifact_ids:
                raise ValueError("result manifest repeats an artifactId")
            head = (snapshot.name, snapshot.version)
            if head in version_heads:
                raise ValueError("result manifest repeats an artifact name/version")
            if snapshot.idempotency_key in idempotency_keys:
                raise ValueError("result manifest repeats an artifact idempotencyKey")
            artifact_ids.add(snapshot.artifact_id)
            version_heads.add(head)
            idempotency_keys.add(snapshot.idempotency_key)
            artifacts.append(snapshot)
        if self.result_ref not in artifact_ids:
            raise ValueError("resultRef must identify one descriptor in artifacts")
        object.__setattr__(self, "artifacts", tuple(artifacts))

    def to_dict(self) -> Dict[str, object]:
        ScopedInvocationResultManifestV2.__post_init__(self)
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
            "artifacts": [item.to_dict() for item in self.artifacts],
        }

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


__all__ = [
    "EMPTY_ACTION_RECEIPT_SET_DIGEST",
    "SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION",
    "ScopedInvocationResultArtifactV2",
    "ScopedInvocationResultManifestV2",
]
