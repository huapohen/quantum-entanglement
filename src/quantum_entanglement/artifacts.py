# ruff: noqa: UP006, UP031, UP035, UP045
"""Append-only artifact version ledger backed by collaboration events."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple, cast
from urllib.parse import quote

from .events import DomainEvent, StoredEvent
from .protocol import ArtifactOutput, ArtifactRef, new_id, utc_now
from .store import ConcurrencyError, SQLiteEventStore

_REPLAY_PAGE_LIMIT = 1_000
_MAX_REPLAY_EVENTS = 1_000_000
_MAX_REPLAY_ARTIFACT_VERSIONS = 100_000
_MAX_REPLAY_CONTENT_BYTES = 256 * 1024 * 1024
_MAX_REPLAY_METADATA_BYTES = 64 * 1024 * 1024
_MAX_REPLAY_METADATA_NODES = 1_000_000
_MAX_REPLAY_STATE_DATA_BYTES = 384 * 1024 * 1024
_MAX_RECORD_ADMISSION_ATTEMPTS = 8
_MAX_IDENTIFIER_LENGTH = 512
_MAX_MEDIA_TYPE_LENGTH = 255
_MAX_URI_LENGTH = 4_096
_MAX_CONTENT_BYTES = 16 * 1024 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_METADATA_DEPTH = 64
_MAX_METADATA_NODES = 10_000
_MAX_METADATA_KEY_LENGTH = 512
_MAX_METADATA_STRING_LENGTH = 65_536
_MAX_METADATA_INTEGER_BITS = 4_096
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CANONICAL_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$")
_PAYLOAD_REQUIRED_KEYS = frozenset({"sessionId", "taskId", "ref", "content", "createdAt"})
_PAYLOAD_OPTIONAL_KEYS = frozenset({"metadata", "trigger"})
_REF_REQUIRED_KEYS = frozenset(
    {
        "artifactId",
        "name",
        "version",
        "mediaType",
        "uri",
        "digest",
        "createdBy",
        "taskId",
    }
)
_REF_OPTIONAL_KEYS = frozenset({"parentVersion"})


class ArtifactReplayError(RuntimeError):
    """Raised when persisted artifact replay cannot be completed safely."""


class ArtifactRecordError(ValueError):
    """Raised before an artifact write when caller input violates its contract."""


@dataclass(frozen=True)
class ArtifactVersion:
    ref: ArtifactRef
    content: str
    metadata: Mapping[str, object]
    created_at: str
    trigger: str


@dataclass(frozen=True)
class _DecodedArtifactVersion:
    key: Tuple[str, str]
    item: ArtifactVersion
    content_bytes: int
    metadata_bytes: int
    metadata_nodes: int
    state_data_bytes: int


@dataclass(frozen=True)
class _ArtifactLedgerUsage:
    artifact_versions: int = 0
    content_bytes: int = 0
    metadata_bytes: int = 0
    metadata_nodes: int = 0
    state_data_bytes: int = 0

    def include(self, decoded: _DecodedArtifactVersion) -> _ArtifactLedgerUsage:
        return _ArtifactLedgerUsage(
            artifact_versions=self.artifact_versions + 1,
            content_bytes=self.content_bytes + decoded.content_bytes,
            metadata_bytes=self.metadata_bytes + decoded.metadata_bytes,
            metadata_nodes=self.metadata_nodes + decoded.metadata_nodes,
            state_data_bytes=self.state_data_bytes + decoded.state_data_bytes,
        )


@dataclass(frozen=True)
class _ArtifactRecordRequest:
    session_id: str
    task_id: str
    agent_id: str
    name: str
    content: str
    media_type: str
    metadata: Dict[str, object]
    correlation_id: Optional[str]
    causation_id: Optional[str]
    trigger: Optional[str]


@dataclass(frozen=True)
class _ArtifactLedgerState:
    versions: Dict[Tuple[str, str], Tuple[ArtifactVersion, ...]]
    usage: _ArtifactLedgerUsage
    replay_position: int


class ArtifactLedger:
    """Owns current versions so worker agents can remain stateless."""

    EVENT_TYPE = "artifact.versioned"

    def __init__(self, event_store: SQLiteEventStore) -> None:
        self.event_store = event_store
        self._state = _ArtifactLedgerState({}, _ArtifactLedgerUsage(), 0)
        self._lock = threading.RLock()
        self._rebuild()

    @property
    def _versions(self) -> Dict[Tuple[str, str], Tuple[ArtifactVersion, ...]]:
        return self._state.versions

    @property
    def _usage(self) -> _ArtifactLedgerUsage:
        return self._state.usage

    @property
    def _replay_position(self) -> int:
        return self._state.replay_position

    def _rebuild(self) -> None:
        with self._lock:
            self._rebuild_under_lock()

    def _rebuild_under_lock(self) -> None:
        after_position = 0
        replayed = 0
        candidate_usage = _ArtifactLedgerUsage()
        candidate_versions: Dict[Tuple[str, str], List[ArtifactVersion]] = {}
        candidate_artifact_ids: set[str] = set()
        while replayed < _MAX_REPLAY_EVENTS:
            page_limit = min(_REPLAY_PAGE_LIMIT, _MAX_REPLAY_EVENTS - replayed)
            page_count = 0
            with self.event_store.stream_all_page(
                after_position=after_position,
                limit=page_limit,
            ) as page:
                for stored in page:
                    page_count += 1
                    if page_count > page_limit:
                        raise ArtifactReplayError(
                            "artifact replay source exceeded its requested page limit"
                        )
                    after_position = self._validate_replay_position(
                        stored,
                        after_position=after_position,
                    )
                    if stored.event.event_type != self.EVENT_TYPE:
                        continue
                    decoded = self._decode_persisted_version_with_usage(stored.event.payload)
                    key = decoded.key
                    item = decoded.item
                    history = candidate_versions.setdefault(key, [])
                    expected_version = len(history) + 1
                    if item.ref.version != expected_version:
                        raise ArtifactReplayError(
                            "artifact replay version chain must start at version 1 "
                            "and increase by one"
                        )
                    if item.ref.artifact_id in candidate_artifact_ids:
                        raise ArtifactReplayError(
                            "artifact replay contains a duplicate artifact id"
                        )

                    next_usage = candidate_usage.include(decoded)
                    self._validate_replay_usage(next_usage)
                    candidate_usage = next_usage
                    candidate_artifact_ids.add(item.ref.artifact_id)
                    history.append(item)
            if page_count == 0:
                break
            replayed += page_count
            if page_count < page_limit:
                break
        else:
            with self.event_store.stream_all_page(after_position=after_position, limit=1) as probe:
                for stored in probe:
                    self._validate_replay_position(stored, after_position=after_position)
                    raise ArtifactReplayError(
                        f"artifact replay exceeds the {_MAX_REPLAY_EVENTS}-event safety limit"
                    )

        self._state = _ArtifactLedgerState(
            versions={key: tuple(history) for key, history in candidate_versions.items()},
            usage=candidate_usage,
            replay_position=after_position,
        )

    @classmethod
    def _decode_persisted_version(
        cls,
        raw_payload: object,
    ) -> Tuple[Tuple[str, str], ArtifactVersion]:
        decoded = cls._decode_persisted_version_with_usage(raw_payload)
        return decoded.key, decoded.item

    @classmethod
    def _decode_persisted_version_with_usage(
        cls,
        raw_payload: object,
    ) -> _DecodedArtifactVersion:
        try:
            payload = cls._require_object_shape(
                raw_payload,
                required=_PAYLOAD_REQUIRED_KEYS,
                optional=_PAYLOAD_OPTIONAL_KEYS,
                field_name="artifact payload",
            )
            session_id = cls._require_text(payload["sessionId"], "sessionId")
            task_id = cls._require_text(payload["taskId"], "taskId")
            content = cls._require_content(payload["content"])
            created_at = cls._require_canonical_utc(payload["createdAt"], "createdAt")
            trigger = cls._require_text(payload.get("trigger", "create"), "trigger")
            decoded_metadata, metadata_bytes, metadata_nodes = cls._decode_metadata_with_usage(
                payload.get("metadata", {})
            )
            metadata = cls._freeze_metadata(decoded_metadata)

            raw_ref = cls._require_object_shape(
                payload["ref"],
                required=_REF_REQUIRED_KEYS,
                optional=_REF_OPTIONAL_KEYS,
                field_name="artifact ref",
            )
            artifact_id = cls._require_text(raw_ref["artifactId"], "ref.artifactId")
            name = cls._require_text(raw_ref["name"], "ref.name")
            version = cls._require_positive_integer(raw_ref["version"], "ref.version")
            media_type = cls._require_text(
                raw_ref["mediaType"],
                "ref.mediaType",
                max_length=_MAX_MEDIA_TYPE_LENGTH,
            )
            uri = cls._require_text(
                raw_ref["uri"],
                "ref.uri",
                max_length=_MAX_URI_LENGTH,
            )
            digest = cls._require_text(
                raw_ref["digest"],
                "ref.digest",
                max_length=71,
            )
            created_by = cls._require_text(raw_ref["createdBy"], "ref.createdBy")
            ref_task_id = cls._require_text(raw_ref["taskId"], "ref.taskId")
            raw_parent = raw_ref.get("parentVersion")
            parent_version = (
                None
                if raw_parent is None
                else cls._require_positive_integer(raw_parent, "ref.parentVersion")
            )
            expected_parent = version - 1 if version > 1 else None
            if parent_version != expected_parent:
                raise ValueError("ref.parentVersion does not match ref.version")
            if ref_task_id != task_id:
                raise ValueError("payload taskId does not match ref.taskId")
            if not _SHA256_PATTERN.fullmatch(digest):
                raise ValueError("ref.digest is not canonical SHA-256")
            if digest != cls._digest(content):
                raise ValueError("ref.digest does not match artifact content")
            if uri != cls._artifact_uri(session_id, name, version):
                raise ValueError("ref.uri is not the canonical artifact URI")

            ref = ArtifactRef(
                artifact_id=artifact_id,
                name=name,
                version=version,
                media_type=media_type,
                uri=uri,
                digest=digest,
                created_by=created_by,
                task_id=ref_task_id,
                parent_version=parent_version,
            )
            item = ArtifactVersion(
                ref=ref,
                content=content,
                metadata=metadata,
                created_at=created_at,
                trigger=trigger,
            )
            descriptor_bytes = sum(
                len(value.encode("utf-8"))
                for value in (
                    session_id,
                    artifact_id,
                    name,
                    media_type,
                    uri,
                    digest,
                    created_by,
                    ref_task_id,
                    created_at,
                    trigger,
                )
            )
            content_bytes = len(content.encode("utf-8"))
            return _DecodedArtifactVersion(
                key=(session_id, name),
                item=item,
                content_bytes=content_bytes,
                metadata_bytes=metadata_bytes,
                metadata_nodes=metadata_nodes,
                state_data_bytes=descriptor_bytes + content_bytes + metadata_bytes,
            )
        except ArtifactReplayError:
            raise
        except (
            KeyError,
            OverflowError,
            RecursionError,
            RuntimeError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise ArtifactReplayError(
                "persisted artifact.versioned payload violates its contract"
            ) from exc

    @staticmethod
    def _require_object_shape(
        value: object,
        *,
        required: frozenset[str],
        optional: frozenset[str],
        field_name: str,
    ) -> Dict[str, Any]:
        if type(value) is not dict:
            raise TypeError(f"{field_name} must be a plain object")
        result = cast(Dict[str, Any], value)
        keys = frozenset(result)
        if not required.issubset(keys) or not keys.issubset(required | optional):
            raise ValueError(f"{field_name} keys do not match its contract")
        return result

    @staticmethod
    def _require_text(
        value: object,
        field_name: str,
        *,
        max_length: int = _MAX_IDENTIFIER_LENGTH,
    ) -> str:
        if type(value) is not str:
            raise TypeError(f"{field_name} must be text")
        if not value.strip():
            raise ValueError(f"{field_name} must not be blank")
        if len(value) > max_length:
            raise ValueError(f"{field_name} exceeds its length limit")
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError(f"{field_name} contains a forbidden control character")
        return value

    @staticmethod
    def _require_positive_integer(value: object, field_name: str) -> int:
        if type(value) is not int:
            raise TypeError(f"{field_name} must be an integer")
        if not 1 <= value <= _MAX_SQLITE_INTEGER:
            raise ValueError(f"{field_name} is outside its supported range")
        return value

    @staticmethod
    def _require_content(value: object) -> str:
        if type(value) is not str:
            raise TypeError("content must be text")
        encoded = value.encode("utf-8")
        if len(encoded) > _MAX_CONTENT_BYTES:
            raise ValueError("content exceeds its byte limit")
        return value

    @staticmethod
    def _require_canonical_utc(value: object, field_name: str) -> str:
        if type(value) is not str or _CANONICAL_UTC_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{field_name} must be canonical UTC")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if canonical != value:
            raise ValueError(f"{field_name} must be canonical UTC")
        return value

    @classmethod
    def _decode_metadata(cls, value: object) -> Dict[str, object]:
        decoded, _encoded_bytes, _nodes = cls._decode_metadata_with_usage(value)
        return decoded

    @classmethod
    def _decode_metadata_with_usage(
        cls,
        value: object,
    ) -> Tuple[Dict[str, object], int, int]:
        if type(value) is not dict:
            raise TypeError("metadata must be a plain JSON object")
        nodes = cls._validate_metadata_json(value)
        encoder = json.JSONEncoder(
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        chunks: list[str] = []
        encoded_size = 0
        for chunk in encoder.iterencode(value):
            encoded_size += len(chunk.encode("utf-8"))
            if encoded_size > _MAX_METADATA_BYTES:
                raise ValueError("metadata exceeds its encoded byte limit")
            chunks.append(chunk)
        encoded = "".join(chunks)
        decoded = json.loads(encoded)
        if type(decoded) is not dict:
            raise TypeError("metadata must decode to a JSON object")
        return cast(Dict[str, object], decoded), encoded_size, nodes

    @classmethod
    def _freeze_metadata(cls, value: Dict[str, object]) -> Mapping[str, object]:
        """Deep-freeze one already validated canonical JSON metadata object."""

        return MappingProxyType(
            {key: cls._freeze_metadata_value(item) for key, item in value.items()}
        )

    @classmethod
    def _freeze_metadata_value(cls, value: object) -> object:
        if type(value) is dict:
            mapping = cast(Dict[str, object], value)
            return MappingProxyType(
                {key: cls._freeze_metadata_value(item) for key, item in mapping.items()}
            )
        if type(value) is list:
            return tuple(cls._freeze_metadata_value(item) for item in cast(list[object], value))
        return value

    @classmethod
    def _snapshot_metadata(cls, value: Mapping[str, object]) -> Dict[str, object]:
        """Return a plain JSON-compatible deep copy of frozen ledger metadata."""

        return {key: cls._snapshot_metadata_value(item) for key, item in value.items()}

    @classmethod
    def _snapshot_metadata_value(cls, value: object) -> object:
        if isinstance(value, Mapping):
            mapping = cast(Mapping[str, object], value)
            return {key: cls._snapshot_metadata_value(item) for key, item in mapping.items()}
        if type(value) is tuple:
            return [cls._snapshot_metadata_value(item) for item in cast(tuple[object, ...], value)]
        return value

    @classmethod
    def _canonical_metadata_bytes(cls, value: Mapping[str, object]) -> bytes:
        return json.dumps(
            cls._snapshot_metadata(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def _snapshot_version(cls, item: ArtifactVersion) -> ArtifactVersion:
        return ArtifactVersion(
            ref=item.ref,
            content=item.content,
            metadata=cls._snapshot_metadata(item.metadata),
            created_at=item.created_at,
            trigger=item.trigger,
        )

    @staticmethod
    def _validate_metadata_json(value: object) -> int:
        stack: list[tuple[object, int, bool]] = [(value, 0, False)]
        active_container_ids: set[int] = set()
        nodes = 0
        while stack:
            current, parent_depth, exiting = stack.pop()
            if exiting:
                active_container_ids.discard(id(current))
                continue

            nodes += 1
            if nodes > _MAX_METADATA_NODES:
                raise ValueError("metadata exceeds its JSON node limit")
            current_type = type(current)
            if current is None or current_type is bool:
                continue
            if current_type is str:
                if len(cast(str, current)) > _MAX_METADATA_STRING_LENGTH:
                    raise ValueError("metadata string exceeds its length limit")
                continue
            if current_type is int:
                if cast(int, current).bit_length() > _MAX_METADATA_INTEGER_BITS:
                    raise ValueError("metadata integer exceeds its bit limit")
                continue
            if current_type is float:
                if not math.isfinite(cast(float, current)):
                    raise ValueError("metadata contains a non-finite number")
                continue
            if current_type not in (dict, list):
                raise TypeError("metadata contains a non-JSON value")

            depth = parent_depth + 1
            if depth > _MAX_METADATA_DEPTH:
                raise ValueError("metadata exceeds its nesting limit")
            identity = id(current)
            if identity in active_container_ids:
                raise ValueError("metadata contains a reference cycle")
            active_container_ids.add(identity)
            stack.append((current, parent_depth, True))

            if current_type is list:
                values = cast(list[object], current)
                if len(values) > _MAX_METADATA_NODES - nodes:
                    raise ValueError("metadata exceeds its JSON node limit")
                items = tuple(values)
                for item in reversed(items):
                    stack.append((item, depth, False))
                continue

            mapping = cast(dict[object, object], current)
            if len(mapping) > (_MAX_METADATA_NODES - nodes) // 2:
                raise ValueError("metadata exceeds its JSON node limit")
            entries = tuple(mapping.items())
            for key, item in reversed(entries):
                if type(key) is not str:
                    raise TypeError("metadata keys must be text")
                if len(key) > _MAX_METADATA_KEY_LENGTH:
                    raise ValueError("metadata key exceeds its length limit")
                nodes += 1
                stack.append((item, depth, False))
        return nodes

    @staticmethod
    def _validate_replay_usage(usage: _ArtifactLedgerUsage) -> None:
        limits = (
            (
                usage.artifact_versions,
                _MAX_REPLAY_ARTIFACT_VERSIONS,
                "artifact-version count",
            ),
            (usage.content_bytes, _MAX_REPLAY_CONTENT_BYTES, "content-byte count"),
            (usage.metadata_bytes, _MAX_REPLAY_METADATA_BYTES, "metadata-byte count"),
            (usage.metadata_nodes, _MAX_REPLAY_METADATA_NODES, "metadata-node count"),
            (
                usage.state_data_bytes,
                _MAX_REPLAY_STATE_DATA_BYTES,
                "state-data byte count",
            ),
        )
        for actual, maximum, label in limits:
            if actual > maximum:
                raise ArtifactReplayError(
                    f"artifact replay exceeds the {maximum} {label} safety limit"
                )

    @staticmethod
    def _validate_replay_position(stored: object, *, after_position: int) -> int:
        position = getattr(stored, "global_position", None)
        if type(position) is not int:
            raise ArtifactReplayError("artifact replay source returned an invalid global position")
        if position <= after_position:
            raise ArtifactReplayError(
                "artifact replay global positions are not strictly increasing"
            )
        return position

    @staticmethod
    def _digest(content: str) -> str:
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _artifact_uri(session_id: str, name: str, version: int) -> str:
        return "artifact://%s/%s/v%d" % (
            quote(session_id, safe=""),
            quote(name),
            version,
        )

    def _resolve_idempotent_version(
        self,
        *,
        key: Tuple[str, str],
        history: Tuple[ArtifactVersion, ...],
        stored: StoredEvent,
        request: _ArtifactRecordRequest,
    ) -> ArtifactVersion:
        if stored.event.event_type != self.EVENT_TYPE:
            raise ArtifactReplayError("idempotent artifact key belongs to another event type")
        existing_key, existing_item = self._decode_persisted_version(stored.event.payload)
        if existing_key != key:
            raise ArtifactReplayError("idempotent artifact event does not match the requested key")
        expected_trigger = request.trigger
        if expected_trigger is None:
            expected_trigger = "create" if existing_item.ref.version == 1 else "revise"
        if (
            stored.event.actor_id != request.agent_id
            or stored.event.correlation_id != request.correlation_id
            or stored.event.causation_id != request.causation_id
            or existing_item.ref.task_id != request.task_id
            or existing_item.ref.created_by != request.agent_id
            or existing_item.ref.name != request.name
            or existing_item.ref.media_type != request.media_type
            or existing_item.content != request.content
            or self._canonical_metadata_bytes(existing_item.metadata)
            != self._canonical_metadata_bytes(request.metadata)
            or existing_item.trigger != expected_trigger
        ):
            raise ArtifactRecordError("idempotent artifact retry changed its request")
        existing_ref = existing_item.ref
        for existing in history:
            if existing.ref.artifact_id == existing_ref.artifact_id:
                return self._snapshot_version(existing)
        self._rebuild_under_lock()
        for existing in self._versions.get(key, ()):
            if existing.ref.artifact_id == existing_ref.artifact_id:
                return self._snapshot_version(existing)
        raise ArtifactReplayError("idempotent artifact event is missing after replay")

    def record(
        self,
        session_id: str,
        task_id: str,
        agent_id: str,
        output: ArtifactOutput,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        trigger: Optional[str] = None,
    ) -> ArtifactVersion:
        with self._lock:
            try:
                if type(output) is not ArtifactOutput:
                    raise TypeError("output must be an ArtifactOutput")
                captured_session_id = self._require_text(session_id, "sessionId")
                captured_task_id = self._require_text(task_id, "taskId")
                captured_agent_id = self._require_text(agent_id, "agentId")
                captured_correlation_id = (
                    None
                    if correlation_id is None
                    else self._require_text(correlation_id, "correlationId")
                )
                captured_causation_id = (
                    None
                    if causation_id is None
                    else self._require_text(causation_id, "causationId")
                )
                captured_name = self._require_text(output.name, "output.name")
                captured_content = self._require_content(output.content)
                captured_media_type = self._require_text(
                    output.media_type,
                    "output.mediaType",
                    max_length=_MAX_MEDIA_TYPE_LENGTH,
                )
                captured_metadata = self._decode_metadata(output.metadata)
                captured_trigger = (
                    None if trigger is None else self._require_text(trigger, "trigger")
                )
                request = _ArtifactRecordRequest(
                    session_id=captured_session_id,
                    task_id=captured_task_id,
                    agent_id=captured_agent_id,
                    name=captured_name,
                    content=captured_content,
                    media_type=captured_media_type,
                    metadata=captured_metadata,
                    correlation_id=captured_correlation_id,
                    causation_id=captured_causation_id,
                    trigger=captured_trigger,
                )
            except (
                ArtifactReplayError,
                AttributeError,
                KeyError,
                OverflowError,
                RecursionError,
                RuntimeError,
                TypeError,
                UnicodeError,
                ValueError,
            ) as exc:
                raise ArtifactRecordError("artifact record input violates its contract") from exc

            key = (request.session_id, request.name)
            stream_id = "session:%s" % request.session_id
            idempotency_key = "artifact:%s:%s:%s" % (
                request.task_id,
                request.name,
                self._digest(request.content),
            )
            for attempt in range(_MAX_RECORD_ADMISSION_ATTEMPTS):
                history = self._versions.get(key, ())
                existing = self.event_store.get_idempotent_event(
                    stream_id,
                    idempotency_key,
                )
                if existing is not None:
                    return self._resolve_idempotent_version(
                        key=key,
                        history=history,
                        stored=existing,
                        request=request,
                    )

                version = len(history) + 1
                actual_trigger = (
                    request.trigger
                    if request.trigger is not None
                    else ("create" if version == 1 else "revise")
                )
                try:
                    ref = ArtifactRef(
                        artifact_id=new_id("art"),
                        name=request.name,
                        version=version,
                        media_type=request.media_type,
                        uri=self._artifact_uri(request.session_id, request.name, version),
                        digest=self._digest(request.content),
                        created_by=request.agent_id,
                        task_id=request.task_id,
                        parent_version=(version - 1 if version > 1 else None),
                    )
                    created_at = self._require_canonical_utc(utc_now(), "createdAt")
                    payload = {
                        "sessionId": request.session_id,
                        "taskId": request.task_id,
                        "ref": ref.to_dict(),
                        "content": request.content,
                        "metadata": request.metadata,
                        "createdAt": created_at,
                        "trigger": actual_trigger,
                    }
                    decoded = self._decode_persisted_version_with_usage(payload)
                    if decoded.key != key:
                        raise RuntimeError("artifact record key changed during validation")
                except (
                    ArtifactReplayError,
                    AttributeError,
                    KeyError,
                    OverflowError,
                    RecursionError,
                    RuntimeError,
                    TypeError,
                    UnicodeError,
                    ValueError,
                ) as exc:
                    raise ArtifactRecordError(
                        "artifact record input violates its contract"
                    ) from exc

                next_usage = self._usage.include(decoded)
                try:
                    self._validate_replay_usage(next_usage)
                except ArtifactReplayError as exc:
                    raise ArtifactRecordError(
                        "artifact record would exceed ledger safety limits"
                    ) from exc

                event = DomainEvent(
                    stream_id=stream_id,
                    event_type=self.EVENT_TYPE,
                    actor_id=request.agent_id,
                    correlation_id=request.correlation_id,
                    causation_id=request.causation_id,
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
                try:
                    stored = self.event_store.append(
                        event,
                        expected_global_position=self._replay_position,
                    )
                except ConcurrencyError as exc:
                    if attempt + 1 >= _MAX_RECORD_ADMISSION_ATTEMPTS:
                        raise ArtifactRecordError(
                            "artifact record admission did not stabilize"
                        ) from exc
                    self._rebuild_under_lock()
                    continue

                # A racing idempotent retry is resolved against its durable request.
                if stored.event.event_id != event.event_id:
                    return self._resolve_idempotent_version(
                        key=key,
                        history=history,
                        stored=stored,
                        request=request,
                    )
                next_versions = dict(self._versions)
                next_versions[key] = history + (decoded.item,)
                self._state = _ArtifactLedgerState(
                    versions=next_versions,
                    usage=next_usage,
                    replay_position=stored.global_position,
                )
                return self._snapshot_version(decoded.item)

            raise AssertionError("artifact admission loop exhausted without a result")

    def current(self, session_id: str, name: str) -> Optional[ArtifactVersion]:
        with self._lock:
            history = self._versions.get((session_id, name), ())
            return self._snapshot_version(history[-1]) if history else None

    def history(self, session_id: str, name: str) -> Tuple[ArtifactVersion, ...]:
        with self._lock:
            return tuple(
                self._snapshot_version(item) for item in self._versions.get((session_id, name), ())
            )

    def current_all(self, session_id: str) -> Tuple[ArtifactVersion, ...]:
        with self._lock:
            return tuple(
                self._snapshot_version(versions[-1])
                for (candidate_session, _), versions in sorted(self._versions.items())
                if candidate_session == session_id and versions
            )

    def by_task(self, session_id: str, task_id: str) -> Tuple[ArtifactVersion, ...]:
        """Return outputs attributed to one task, rebuilt from the append-only ledger."""

        with self._lock:
            items = [
                item
                for (candidate_session, _), versions in self._versions.items()
                if candidate_session == session_id
                for item in versions
                if item.ref.task_id == task_id
            ]
            items.sort(key=lambda item: (item.ref.name, item.ref.version))
            return tuple(self._snapshot_version(item) for item in items)

    def restore(
        self,
        session_id: str,
        name: str,
        target_version: int,
        task_id: str,
        actor_id: str,
    ) -> ArtifactVersion:
        history = self.history(session_id, name)
        if target_version <= 0 or target_version > len(history):
            raise KeyError("artifact version not found: %s v%d" % (name, target_version))
        source = history[target_version - 1]
        return self.record(
            session_id=session_id,
            task_id=task_id,
            agent_id=actor_id,
            output=ArtifactOutput(
                name=name,
                content=source.content,
                media_type=source.ref.media_type,
                metadata={"restoredFrom": target_version},
            ),
            trigger="rollback",
        )


__all__ = [
    "ArtifactLedger",
    "ArtifactRecordError",
    "ArtifactReplayError",
    "ArtifactVersion",
]
