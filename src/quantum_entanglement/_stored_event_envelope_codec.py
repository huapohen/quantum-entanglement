"""Private canonical codec for one exact durable event-row identity.

This module is deliberately not exported from :mod:`quantum_entanglement`.  Its values are
capability-free: a valid envelope or digest does not prove that a row is durable, accepted, or
authorized.  The future result writer must derive one envelope from its frozen INSERT snapshot
and another from the exact raw SQLite row, then compare them inside the owning transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, cast

STORED_EVENT_ENVELOPE_SCHEMA_VERSION = 1
STORED_EVENT_ENVELOPE_DOMAIN = "quantum-entanglement.stored-event-envelope/1\n"

_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_IDENTITY_BYTES = 4_096
_MAX_PAYLOAD_BYTES = 1_048_576
_MAX_PAYLOAD_DEPTH = 64
_MAX_PAYLOAD_NODES = 10_000
_MAX_PAYLOAD_KEY_CHARACTERS = 512
_MAX_PAYLOAD_STRING_CHARACTERS = 65_536
_CANONICAL_UTC_MICROSECONDS = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z"
)
_RAW_EVENT_ROW_COLUMNS = (
    "global_position",
    "stream_id",
    "sequence",
    "event_id",
    "event_type",
    "actor_id",
    "timestamp",
    "payload_json",
    "correlation_id",
    "causation_id",
    "idempotency_key",
)


class StoredEventEnvelopeError(ValueError):
    """A candidate envelope does not have one canonical, bounded representation."""


class StoredEventEnvelopeTypeError(StoredEventEnvelopeError, TypeError):
    """A candidate envelope uses a runtime or SQLite type outside the exact contract."""


class StoredEventEnvelopeCanonicalError(StoredEventEnvelopeError):
    """A candidate envelope is valid JSON-like data but is not canonical."""


def _plain_text(value: object, field: str) -> str:
    if type(value) is not str:
        raise StoredEventEnvelopeTypeError(f"{field} must be a plain string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise StoredEventEnvelopeError(f"{field} must be valid UTF-8") from None
    if not encoded or len(encoded) > _MAX_IDENTITY_BYTES:
        raise StoredEventEnvelopeError(f"{field} violates its UTF-8 byte limit")
    if value != value.strip():
        raise StoredEventEnvelopeCanonicalError(f"{field} has surrounding whitespace")
    if unicodedata.normalize("NFC", value) != value:
        raise StoredEventEnvelopeCanonicalError(f"{field} must use Unicode NFC")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise StoredEventEnvelopeCanonicalError(f"{field} contains a forbidden control character")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _plain_text(value, field)


def _payload_text(value: str, field: str, *, maximum_characters: int) -> None:
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise StoredEventEnvelopeError(f"{field} must be valid UTF-8") from None
    if len(value) > maximum_characters:
        raise StoredEventEnvelopeError(f"{field} exceeds its character limit")
    if unicodedata.normalize("NFC", value) != value:
        raise StoredEventEnvelopeCanonicalError(f"{field} must use Unicode NFC")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise StoredEventEnvelopeCanonicalError(f"{field} contains a forbidden control character")


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise StoredEventEnvelopeTypeError(f"{field} must be a plain integer")
    if value < 1 or value > _MAX_SQLITE_INTEGER:
        raise StoredEventEnvelopeError(f"{field} is outside the positive signed-64 range")
    return value


def _timestamp(value: object) -> str:
    snapshot = _plain_text(value, "timestamp")
    if _CANONICAL_UTC_MICROSECONDS.fullmatch(snapshot) is None:
        raise StoredEventEnvelopeCanonicalError(
            "timestamp must use canonical UTC microseconds"
        )
    try:
        parsed = datetime.strptime(snapshot, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise StoredEventEnvelopeCanonicalError(
            "timestamp must use canonical UTC microseconds"
        ) from None
    if parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != snapshot:
        raise StoredEventEnvelopeCanonicalError(
            "timestamp must use canonical UTC microseconds"
        )
    return snapshot


def _reject_json_constant(_value: str) -> Any:
    raise StoredEventEnvelopeCanonicalError("payload contains a non-finite number")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StoredEventEnvelopeCanonicalError("payload contains a duplicate object key")
        result[key] = value
    return result


class _PayloadTraversal:
    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = 0


def _validate_payload_value(
    value: object,
    *,
    path: str,
    depth: int,
    traversal: _PayloadTraversal,
) -> None:
    if depth > _MAX_PAYLOAD_DEPTH:
        raise StoredEventEnvelopeError("payload exceeds its depth limit")
    traversal.nodes += 1
    if traversal.nodes > _MAX_PAYLOAD_NODES:
        raise StoredEventEnvelopeError("payload exceeds its node limit")

    value_type = type(value)
    if value is None or value_type is bool:
        return
    if value_type is str:
        _payload_text(
            cast(str, value),
            path,
            maximum_characters=_MAX_PAYLOAD_STRING_CHARACTERS,
        )
        return
    if value_type is int:
        if cast(int, value).bit_length() > 4_096:
            raise StoredEventEnvelopeError("payload integer exceeds its bit limit")
        return
    if value_type is float:
        if not math.isfinite(cast(float, value)):
            raise StoredEventEnvelopeCanonicalError("payload contains a non-finite number")
        return
    if value_type is list:
        for index, item in enumerate(cast(list[Any], value)):
            _validate_payload_value(
                item,
                path=f"payload[{index}]",
                depth=depth + 1,
                traversal=traversal,
            )
        return
    if value_type is dict:
        for key, item in cast(dict[Any, Any], value).items():
            if type(key) is not str:
                raise StoredEventEnvelopeTypeError("payload keys must be plain strings")
            _payload_text(
                key,
                "payload key",
                maximum_characters=_MAX_PAYLOAD_KEY_CHARACTERS,
            )
            _validate_payload_value(
                item,
                path=f"payload.{key}",
                depth=depth + 1,
                traversal=traversal,
            )
        return
    raise StoredEventEnvelopeTypeError("payload contains an unsupported JSON runtime type")


def _canonical_payload(encoded: object) -> tuple[dict[str, Any], str]:
    if type(encoded) is not str:
        raise StoredEventEnvelopeTypeError("payload_json must use SQLite TEXT storage")
    try:
        payload_bytes = encoded.encode("utf-8")
    except UnicodeError:
        raise StoredEventEnvelopeError("payload_json must be valid UTF-8") from None
    if not payload_bytes or len(payload_bytes) > _MAX_PAYLOAD_BYTES:
        raise StoredEventEnvelopeError("payload_json violates its encoded byte limit")
    try:
        decoded = json.loads(
            encoded,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except StoredEventEnvelopeError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise StoredEventEnvelopeCanonicalError("payload_json is not canonical JSON") from None
    if type(decoded) is not dict:
        raise StoredEventEnvelopeTypeError("payload root must be a plain JSON object")
    _validate_payload_value(
        decoded,
        path="payload",
        depth=0,
        traversal=_PayloadTraversal(),
    )
    canonical = json.dumps(
        decoded,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if canonical != encoded:
        raise StoredEventEnvelopeCanonicalError("payload_json bytes are not canonical")
    return cast(dict[str, Any], decoded), canonical


class _StoredEventEnvelopeV1:
    """Immutable, capability-free snapshot of one exact stored event row."""

    __slots__ = (
        "__actor_id",
        "__causation_id",
        "__correlation_id",
        "__event_id",
        "__event_type",
        "__global_position",
        "__idempotency_key",
        "__payload_json",
        "__sequence",
        "__stream_id",
        "__timestamp",
    )

    def __init__(
        self,
        *,
        event_id: object,
        stream_id: object,
        event_type: object,
        actor_id: object,
        timestamp: object,
        correlation_id: object,
        causation_id: object,
        idempotency_key: object,
        payload_json: object,
        sequence: object,
        global_position: object,
    ) -> None:
        if type(self) is not _StoredEventEnvelopeV1:
            raise StoredEventEnvelopeTypeError("stored event envelope must use its exact class")
        _payload, canonical_payload = _canonical_payload(payload_json)
        object.__setattr__(
            self,
            "_StoredEventEnvelopeV1__event_id",
            _plain_text(event_id, "event_id"),
        )
        object.__setattr__(
            self, "_StoredEventEnvelopeV1__stream_id", _plain_text(stream_id, "stream_id")
        )
        object.__setattr__(
            self, "_StoredEventEnvelopeV1__event_type", _plain_text(event_type, "event_type")
        )
        object.__setattr__(
            self,
            "_StoredEventEnvelopeV1__actor_id",
            _plain_text(actor_id, "actor_id"),
        )
        object.__setattr__(self, "_StoredEventEnvelopeV1__timestamp", _timestamp(timestamp))
        object.__setattr__(
            self,
            "_StoredEventEnvelopeV1__correlation_id",
            _optional_text(correlation_id, "correlation_id"),
        )
        object.__setattr__(
            self,
            "_StoredEventEnvelopeV1__causation_id",
            _optional_text(causation_id, "causation_id"),
        )
        object.__setattr__(
            self,
            "_StoredEventEnvelopeV1__idempotency_key",
            _optional_text(idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "_StoredEventEnvelopeV1__payload_json", canonical_payload)
        object.__setattr__(
            self, "_StoredEventEnvelopeV1__sequence", _positive_integer(sequence, "sequence")
        )
        object.__setattr__(
            self,
            "_StoredEventEnvelopeV1__global_position",
            _positive_integer(global_position, "global_position"),
        )

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("stored event envelope is immutable")

    def __repr__(self) -> str:
        return "_StoredEventEnvelopeV1(<capability-free>)"

    def to_dict(self) -> dict[str, Any]:
        snapshot = _snapshot_envelope(self)
        payload, _encoded = _canonical_payload(
            object.__getattribute__(snapshot, "_StoredEventEnvelopeV1__payload_json")
        )
        return {
            "schemaVersion": STORED_EVENT_ENVELOPE_SCHEMA_VERSION,
            "eventId": object.__getattribute__(snapshot, "_StoredEventEnvelopeV1__event_id"),
            "streamId": object.__getattribute__(snapshot, "_StoredEventEnvelopeV1__stream_id"),
            "eventType": object.__getattribute__(snapshot, "_StoredEventEnvelopeV1__event_type"),
            "actorId": object.__getattribute__(snapshot, "_StoredEventEnvelopeV1__actor_id"),
            "timestamp": object.__getattribute__(snapshot, "_StoredEventEnvelopeV1__timestamp"),
            "correlationId": object.__getattribute__(
                snapshot, "_StoredEventEnvelopeV1__correlation_id"
            ),
            "causationId": object.__getattribute__(
                snapshot, "_StoredEventEnvelopeV1__causation_id"
            ),
            "idempotencyKey": object.__getattribute__(
                snapshot, "_StoredEventEnvelopeV1__idempotency_key"
            ),
            "payload": payload,
            "sequence": object.__getattribute__(snapshot, "_StoredEventEnvelopeV1__sequence"),
            "globalPosition": object.__getattribute__(
                snapshot, "_StoredEventEnvelopeV1__global_position"
            ),
        }

    def canonical_bytes(self) -> bytes:
        body = _StoredEventEnvelopeV1.to_dict(self)
        return json.dumps(
            body,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(
            STORED_EVENT_ENVELOPE_DOMAIN.encode("utf-8")
            + _StoredEventEnvelopeV1.canonical_bytes(self)
        ).hexdigest()


def _snapshot_envelope(value: object) -> _StoredEventEnvelopeV1:
    if type(value) is not _StoredEventEnvelopeV1:
        raise StoredEventEnvelopeTypeError("envelope must use its exact class")
    try:
        return _StoredEventEnvelopeV1(
            event_id=object.__getattribute__(value, "_StoredEventEnvelopeV1__event_id"),
            stream_id=object.__getattribute__(value, "_StoredEventEnvelopeV1__stream_id"),
            event_type=object.__getattribute__(value, "_StoredEventEnvelopeV1__event_type"),
            actor_id=object.__getattribute__(value, "_StoredEventEnvelopeV1__actor_id"),
            timestamp=object.__getattribute__(value, "_StoredEventEnvelopeV1__timestamp"),
            correlation_id=object.__getattribute__(
                value, "_StoredEventEnvelopeV1__correlation_id"
            ),
            causation_id=object.__getattribute__(value, "_StoredEventEnvelopeV1__causation_id"),
            idempotency_key=object.__getattribute__(
                value, "_StoredEventEnvelopeV1__idempotency_key"
            ),
            payload_json=object.__getattribute__(
                value, "_StoredEventEnvelopeV1__payload_json"
            ),
            sequence=object.__getattribute__(value, "_StoredEventEnvelopeV1__sequence"),
            global_position=object.__getattribute__(
                value, "_StoredEventEnvelopeV1__global_position"
            ),
        )
    except StoredEventEnvelopeError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise StoredEventEnvelopeCanonicalError("stored event envelope is malformed") from None


def _stored_event_envelope_from_values(
    *,
    event_id: object,
    stream_id: object,
    event_type: object,
    actor_id: object,
    timestamp: object,
    correlation_id: object,
    causation_id: object,
    idempotency_key: object,
    payload_json: object,
    sequence: object,
    global_position: object,
) -> _StoredEventEnvelopeV1:
    """Build a private envelope from values already frozen by an owning store."""

    return _StoredEventEnvelopeV1(
        event_id=event_id,
        stream_id=stream_id,
        event_type=event_type,
        actor_id=actor_id,
        timestamp=timestamp,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=idempotency_key,
        payload_json=payload_json,
        sequence=sequence,
        global_position=global_position,
    )


def _stored_event_envelope_from_raw_row(row: object) -> _StoredEventEnvelopeV1:
    """Recompute an envelope from one exact raw SQLite row without a read model."""

    if type(row) is not sqlite3.Row:
        raise StoredEventEnvelopeTypeError("raw event row must be an exact sqlite3.Row")
    try:
        keys: Sequence[str] = row.keys()
        if tuple(keys) != _RAW_EVENT_ROW_COLUMNS:
            raise StoredEventEnvelopeCanonicalError("raw event row columns are not exact")
        return _stored_event_envelope_from_values(
            event_id=row["event_id"],
            stream_id=row["stream_id"],
            event_type=row["event_type"],
            actor_id=row["actor_id"],
            timestamp=row["timestamp"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            idempotency_key=row["idempotency_key"],
            payload_json=row["payload_json"],
            sequence=row["sequence"],
            global_position=row["global_position"],
        )
    except StoredEventEnvelopeError:
        raise
    except (IndexError, KeyError, TypeError, ValueError):
        raise StoredEventEnvelopeCanonicalError("raw event row is malformed") from None
