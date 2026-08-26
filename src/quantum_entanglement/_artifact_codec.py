# ruff: noqa: UP006, UP035
"""Pure canonical codecs shared by Artifact persistence and result acceptance.

The existing Artifact row request digest is intentionally preserved byte-for-byte for database
compatibility.  Stronger result-acceptance identities use their own domain-separated digest and
must cover the fields that the legacy row fingerprint omits.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, Mapping, cast

ARTIFACT_METADATA_DOMAIN_V1 = "quantum-entanglement.artifact-metadata/1\n"
MAX_ARTIFACT_METADATA_BYTES = 65_536
MAX_ARTIFACT_METADATA_DEPTH = 64
MAX_ARTIFACT_METADATA_NODES = 10_000
MAX_ARTIFACT_METADATA_KEY_BYTES = 512
MAX_ARTIFACT_METADATA_STRING_BYTES = 65_536
MAX_ARTIFACT_METADATA_INTEGER_BITS = 4_096

_SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_BLOB_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAPPING_PROXY_TYPE: type = type(MappingProxyType({}))


class ArtifactMetadataCodecTooLargeError(ValueError):
    """Raised when canonical Artifact metadata exceeds a structural or byte bound."""


def _safe_text(value: object, label: str, *, maximum_bytes: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a plain string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        encoded = None
    if encoded is None:
        raise ValueError(f"{label} must be valid UTF-8") from None
    if len(encoded) > maximum_bytes:
        raise ArtifactMetadataCodecTooLargeError(f"{label} exceeds its UTF-8 byte limit")
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
    if nodes[0] > MAX_ARTIFACT_METADATA_NODES:
        raise ArtifactMetadataCodecTooLargeError(f"{label} exceeds its JSON node limit")
    value_type = type(value)
    if value is None or value_type is bool:
        return value
    if value_type is str:
        return _safe_text(value, label, maximum_bytes=MAX_ARTIFACT_METADATA_STRING_BYTES)
    if value_type is int:
        integer = cast(int, value)
        if integer.bit_length() > MAX_ARTIFACT_METADATA_INTEGER_BITS:
            raise ArtifactMetadataCodecTooLargeError(f"{label} integer exceeds its bit limit")
        return integer
    if value_type is float:
        number = cast(float, value)
        if not math.isfinite(number):
            raise ValueError(f"{label} contains a non-finite number")
        return number
    if value_type not in (dict, list):
        raise TypeError(f"{label} contains a non-JSON value")
    if depth >= MAX_ARTIFACT_METADATA_DEPTH:
        raise ArtifactMetadataCodecTooLargeError(f"{label} exceeds its nesting limit")
    identity = id(value)
    if identity in active:
        raise ValueError(f"{label} contains a reference cycle")
    active.add(identity)
    try:
        if value_type is list:
            sequence = cast(list[object], value)
            try:
                captured_items = tuple(sequence)
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
                for index, item in enumerate(captured_items)
            ]
            try:
                current_items = tuple(sequence)
            except RuntimeError as error:
                raise ValueError(f"{label} changed while it was being snapshotted") from error
            if len(current_items) != len(captured_items) or any(
                not _same_snapshot_slot(current, captured)
                for current, captured in zip(current_items, captured_items)
            ):
                raise ValueError(f"{label} changed while it was being snapshotted")
            return copied

        mapping = cast(dict[object, object], value)
        try:
            captured_entries = tuple(mapping.items())
        except RuntimeError as error:
            raise ValueError(f"{label} changed while it was being snapshotted") from error
        copied_mapping: Dict[str, object] = {}
        for key, item in captured_entries:
            nodes[0] += 1
            if nodes[0] > MAX_ARTIFACT_METADATA_NODES:
                raise ArtifactMetadataCodecTooLargeError(f"{label} exceeds its JSON node limit")
            normalized_key = _safe_text(
                key,
                f"{label} key",
                maximum_bytes=MAX_ARTIFACT_METADATA_KEY_BYTES,
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
        if len(current_entries) != len(captured_entries) or any(
            current_key != captured_key or not _same_snapshot_slot(current_value, captured_value)
            for (current_key, current_value), (captured_key, captured_value) in zip(
                current_entries,
                captured_entries,
            )
        ):
            raise ValueError(f"{label} changed while it was being snapshotted")
        return copied_mapping
    finally:
        active.discard(identity)


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        mapping = cast(Dict[str, object], value)
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


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True)
class CanonicalArtifactMetadataV1:
    """An immutable canonical JSON object and its exact retained bytes."""

    canonical_bytes: bytes = field(repr=False)
    value: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not CanonicalArtifactMetadataV1:
            raise TypeError("metadata snapshot must use the exact schema-1 class")
        if type(self.canonical_bytes) is not bytes:
            raise TypeError("canonical metadata must be immutable bytes")
        if type(self.value) is not _MAPPING_PROXY_TYPE:
            raise TypeError("canonical metadata value must be a frozen object")
        if _canonical_json_bytes(cast(Mapping[str, object], _thaw_json(self.value))) != (
            self.canonical_bytes
        ):
            raise ValueError("canonical metadata bytes and value disagree")

    def to_dict(self) -> Dict[str, object]:
        thawed = _thaw_json(self.value)
        if type(thawed) is not dict:  # pragma: no cover - protected by construction.
            raise TypeError("canonical metadata is not a JSON object")
        return cast(Dict[str, object], thawed)


def canonical_artifact_metadata_v1(
    metadata: object,
    *,
    maximum_bytes: int = MAX_ARTIFACT_METADATA_BYTES,
) -> CanonicalArtifactMetadataV1:
    if type(maximum_bytes) is not int:
        raise TypeError("maximum_bytes must be an exact integer")
    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be greater than zero")
    if type(metadata) is not dict:
        raise TypeError("metadata must be a plain dictionary")
    snapshot = _snapshot_json_value(metadata, "metadata", depth=0, active=set(), nodes=[0])
    if type(snapshot) is not dict:  # pragma: no cover - protected by the exact root guard.
        raise TypeError("metadata must be a JSON object")
    canonical_bytes = _canonical_json_bytes(cast(Mapping[str, object], snapshot))
    if len(canonical_bytes) > maximum_bytes:
        raise ArtifactMetadataCodecTooLargeError("metadata exceeds its canonical byte limit")
    return CanonicalArtifactMetadataV1(
        canonical_bytes=canonical_bytes,
        value=cast(Mapping[str, object], _freeze_json(snapshot)),
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"canonical metadata contains unsupported JSON constant {value}")


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> Dict[str, object]:
    value: Dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("canonical metadata contains a duplicate object key")
        value[key] = item
    return value


def decode_canonical_artifact_metadata_v1(
    encoded: object,
    *,
    maximum_bytes: int = MAX_ARTIFACT_METADATA_BYTES,
) -> CanonicalArtifactMetadataV1:
    if type(encoded) is not bytes:
        raise TypeError("encoded metadata must be immutable bytes")
    if len(encoded) > maximum_bytes:
        raise ArtifactMetadataCodecTooLargeError("metadata exceeds its canonical byte limit")
    try:
        text = encoded.decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("encoded metadata is not bounded canonical JSON") from error
    canonical = canonical_artifact_metadata_v1(decoded, maximum_bytes=maximum_bytes)
    if canonical.canonical_bytes != encoded:
        raise ValueError("encoded metadata is not in canonical form")
    return canonical


def artifact_blob_digest_v1(content: object) -> str:
    if type(content) is not bytes:
        raise TypeError("artifact content must be immutable bytes")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def artifact_metadata_digest_v1(metadata: CanonicalArtifactMetadataV1) -> str:
    if type(metadata) is not CanonicalArtifactMetadataV1:
        raise TypeError("metadata must be exact CanonicalArtifactMetadataV1")
    return hashlib.sha256(
        ARTIFACT_METADATA_DOMAIN_V1.encode("utf-8") + metadata.canonical_bytes
    ).hexdigest()


def artifact_request_digest_v1(
    *,
    tenant_id: str,
    workspace_id: str,
    session_id: str,
    task_id: str,
    name: str,
    media_type: str,
    blob_digest: str,
    byte_size: int,
    metadata: CanonicalArtifactMetadataV1,
    created_by: str,
) -> str:
    for label, value in (
        ("tenant_id", tenant_id),
        ("workspace_id", workspace_id),
        ("session_id", session_id),
        ("task_id", task_id),
        ("name", name),
        ("media_type", media_type),
        ("created_by", created_by),
    ):
        if type(value) is not str or not value:
            raise ValueError(f"{label} must be non-empty plain text")
    if type(blob_digest) is not str or _BLOB_DIGEST_PATTERN.fullmatch(blob_digest) is None:
        raise ValueError("blob_digest must be canonical sha256:<lowercase-hex>")
    if type(byte_size) is not int:
        raise TypeError("byte_size must be an exact integer")
    if byte_size < 0:
        raise ValueError("byte_size cannot be negative")
    if type(metadata) is not CanonicalArtifactMetadataV1:
        raise TypeError("metadata must be exact CanonicalArtifactMetadataV1")
    payload = {
        "tenantId": tenant_id,
        "workspaceId": workspace_id,
        "sessionId": session_id,
        "taskId": task_id,
        "name": name,
        "mediaType": media_type,
        "blobDigest": blob_digest,
        "byteSize": byte_size,
        "metadata": metadata.to_dict(),
        "createdBy": created_by,
    }
    encoded = _canonical_json_bytes(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    if _SHA256_HEX_PATTERN.fullmatch(digest) is None:  # pragma: no cover - hashlib guarantee.
        raise ValueError("artifact request digest is not canonical SHA-256")
    return digest
