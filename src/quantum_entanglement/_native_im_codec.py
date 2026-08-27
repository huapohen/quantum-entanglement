# ruff: noqa: UP006, UP035
"""Private strict primitives for the frozen native-IM V1 wire contract.

The helpers in this module are deliberately side-effect free.  They do not inspect the
environment, resolve endpoints, open transports, or turn decoded data into authority.
Model modules must perform exact typed decode before using the digest helpers.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Set, Tuple, cast

NATIVE_IM_SCHEMA_VERSION = 1
MAX_ID_BYTES = 4_096
MAX_DISPLAY_TEXT_BYTES = 16 * 1_024
MAX_MESSAGE_TEXT_BYTES = 1 * 1_024 * 1_024
MAX_SIGNED_64 = (1 << 63) - 1
MIN_SIGNED_64 = -(1 << 63)

_CANONICAL_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TRACEPARENT_PATTERN = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-(00|01)\Z")
_MEDIA_TYPE_PATTERN = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+*-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+*-]{0,126}\Z"
)


class NativeIMCodecTooLargeError(ValueError):
    """A native-IM V1 value exceeded a frozen structural or byte bound."""


def _plain_dict(value: object, fields: Set[str], label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a plain dictionary")
    typed = cast(Dict[str, Any], value)
    keys = tuple(typed)
    if any(type(key) is not str for key in keys):
        raise TypeError(f"{label} keys must be plain strings")
    if set(keys) != fields:
        raise ValueError(f"{label} fields do not match its exact schema version")
    return typed


def _plain_list(value: object, label: str, *, maximum_items: int) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a plain list")
    if type(maximum_items) is not int:
        raise TypeError("maximum_items must be an exact integer")
    if maximum_items < 0:
        raise ValueError("maximum_items must be non-negative")
    typed = cast(list[object], value)
    if len(typed) > maximum_items:
        raise NativeIMCodecTooLargeError(f"{label} exceeds its item limit")
    return typed


def _schema_version(value: object, label: str = "schemaVersion") -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    if value != NATIVE_IM_SCHEMA_VERSION:
        raise ValueError(f"{label} must equal {NATIVE_IM_SCHEMA_VERSION}")
    return value


def _utf8_text(
    value: object,
    label: str,
    *,
    maximum_bytes: int,
    allow_empty: bool,
    allow_message_controls: bool,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a plain string")
    if not allow_empty and not value:
        raise ValueError(f"{label} must be non-empty")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{label} must not contain a surrogate code point") from None
    if len(encoded) > maximum_bytes:
        raise NativeIMCodecTooLargeError(f"{label} exceeds its UTF-8 byte limit")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must use Unicode NFC")
    for character in value:
        codepoint = ord(character)
        if codepoint == 0x7F:
            raise ValueError(f"{label} contains a forbidden control character")
        if codepoint < 0x20 and not (allow_message_controls and character in ("\t", "\n")):
            raise ValueError(f"{label} contains a forbidden control character")
    return value


def _id(value: object, label: str) -> str:
    return _utf8_text(
        value,
        label,
        maximum_bytes=MAX_ID_BYTES,
        allow_empty=False,
        allow_message_controls=False,
    )


def _display_text(value: object, label: str) -> str:
    return _utf8_text(
        value,
        label,
        maximum_bytes=MAX_DISPLAY_TEXT_BYTES,
        allow_empty=True,
        allow_message_controls=False,
    )


def _message_text(value: object, label: str, *, allow_empty: bool = True) -> str:
    return _utf8_text(
        value,
        label,
        maximum_bytes=MAX_MESSAGE_TEXT_BYTES,
        allow_empty=allow_empty,
        allow_message_controls=True,
    )


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    if not MIN_SIGNED_64 <= value <= MAX_SIGNED_64:
        raise ValueError(f"{label} is outside the signed 64-bit range")
    return value


def _non_negative_integer(value: object, label: str) -> int:
    integer = _integer(value, label)
    if integer < 0:
        raise ValueError(f"{label} must be non-negative")
    return integer


def _positive_integer(value: object, label: str) -> int:
    integer = _integer(value, label)
    if integer <= 0:
        raise ValueError(f"{label} must be positive")
    return integer


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact boolean")
    return value


def _timestamp(value: object, label: str) -> str:
    timestamp = _utf8_text(
        value,
        label,
        maximum_bytes=27,
        allow_empty=False,
        allow_message_controls=False,
    )
    if _CANONICAL_TIMESTAMP_PATTERN.fullmatch(timestamp) is None:
        raise ValueError(f"{label} must be canonical UTC with microseconds")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"{label} must be a valid UTC timestamp") from None
    canonical = (
        parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    if canonical != timestamp:
        raise ValueError(f"{label} must be canonical UTC with microseconds")
    return timestamp


def _digest(value: object, label: str) -> str:
    digest = _utf8_text(
        value,
        label,
        maximum_bytes=64,
        allow_empty=False,
        allow_message_controls=False,
    )
    if _DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} must be canonical lowercase SHA-256")
    return digest


def _traceparent(value: object, label: str) -> str:
    traceparent = _utf8_text(
        value,
        label,
        maximum_bytes=55,
        allow_empty=False,
        allow_message_controls=False,
    )
    match = _TRACEPARENT_PATTERN.fullmatch(traceparent)
    if match is None or set(match.group(1)) == {"0"} or set(match.group(2)) == {"0"}:
        raise ValueError(f"{label} must be a canonical non-zero W3C traceparent")
    return traceparent


def _media_type(value: object, label: str) -> str:
    media_type = _utf8_text(
        value,
        label,
        maximum_bytes=255,
        allow_empty=False,
        allow_message_controls=False,
    )
    if _MEDIA_TYPE_PATTERN.fullmatch(media_type) is None:
        raise ValueError(f"{label} must be a canonical lowercase media type")
    return media_type


def _enum(value: object, allowed: Set[str], label: str) -> str:
    enum_value = _id(value, label)
    if enum_value not in allowed:
        raise ValueError(f"{label} is not a supported V1 value")
    return enum_value


def _ordered_unique_text(values: Iterable[str], label: str) -> Tuple[str, ...]:
    captured = tuple(values)
    if len(set(captured)) != len(captured):
        raise ValueError(f"{label} must not contain duplicates")
    if captured != tuple(sorted(captured, key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{label} must use canonical UTF-8 lexical order")
    return captured


def _canonical_json_bytes(body: Dict[str, Any]) -> bytes:
    if type(body) is not dict:
        raise TypeError("canonical JSON body must be a plain dictionary")
    return json.dumps(
        body,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _model_digest(model_name: str, body: Dict[str, Any]) -> str:
    if type(model_name) is not str or not model_name.isascii() or not model_name:
        raise TypeError("model_name must be non-empty ASCII text")
    domain = f"quantum-entanglement.native-im/{model_name}/1\n".encode()
    return hashlib.sha256(domain + _canonical_json_bytes(body)).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError("native-IM JSON contains an unsupported numeric constant")


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> Dict[str, object]:
    value: Dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("native-IM JSON contains a duplicate object key")
        value[key] = item
    return value


def _decode_json_bytes(encoded: object, label: str, *, maximum_bytes: int) -> object:
    if type(encoded) is not bytes:
        raise TypeError(f"{label} must be immutable bytes")
    if type(maximum_bytes) is not int:
        raise TypeError("maximum_bytes must be an exact integer")
    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be positive")
    if len(encoded) > maximum_bytes:
        raise NativeIMCodecTooLargeError(f"{label} exceeds its top-level byte limit")
    try:
        text = encoded.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except UnicodeError:
        raise ValueError(f"{label} must be strict UTF-8 JSON") from None
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError(f"{label} must be valid bounded JSON") from error
