# ruff: noqa: UP006, UP035
"""Zero-network provider exchange seam for offline native-IM transport conformance.

The values in this module describe an already-authorized request intent and an ephemeral
wire response. No HTTP client, socket, DNS resolver, credential provider, endpoint
registration, or default composition exists here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, Protocol, runtime_checkable

from ._native_im_codec import (
    NativeIMCodecTooLargeError,
    _canonical_json_bytes,
    _decode_json_bytes,
    _digest,
    _enum,
    _id,
    _model_digest,
    _plain_dict,
    _positive_integer,
    _schema_version,
    _timestamp,
    _utf8_text,
)
from .service.native_im_config import CanonicalAbsolutePath, CanonicalHTTPSOrigin
from .service.secrets import SecretMaterial

_MAX_INTENT_BYTES = 64 * 1_024
_MAX_WIRE_BODY_BYTES = 16 * 1_024 * 1_024
_MAX_HEADERS = 128
_MAX_QUERY_ITEMS = 64
_HEADER_NAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?")
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authenticate",
    "proxy-authorization",
    "set-cookie",
    "www-authenticate",
}
_INTENT_FIELDS = {
    "connectTimeoutMs",
    "maxResponseBytes",
    "method",
    "operation",
    "origin",
    "path",
    "query",
    "readRequestDigest",
    "readRequestId",
    "readTimeoutMs",
    "redirectMode",
    "schemaVersion",
}


def _optional_id(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _id(value, label)


def _optional_digest(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _digest(value, label)


def _query_items(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple or len(value) > _MAX_QUERY_ITEMS:
        raise TypeError("provider request query must be a bounded immutable tuple")
    result: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if type(item) is not tuple or len(item) != 2:
            raise TypeError(f"provider request query[{index}] must be an exact pair")
        name = _utf8_text(
            item[0],
            f"query[{index}].name",
            maximum_bytes=256,
            allow_empty=False,
            allow_message_controls=False,
        )
        text = _utf8_text(
            item[1],
            f"query[{index}].value",
            maximum_bytes=8_192,
            allow_empty=True,
            allow_message_controls=False,
        )
        result.append((name, text))
    if tuple(result) != tuple(sorted(result)) or len({name for name, _ in result}) != len(result):
        raise ValueError("provider request query must be sorted with unique names")
    return tuple(result)


def _headers(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple or len(value) > _MAX_HEADERS:
        raise TypeError("provider wire headers must be a bounded immutable tuple")
    result: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if type(item) is not tuple or len(item) != 2:
            raise TypeError(f"provider wire headers[{index}] must be an exact pair")
        name = _utf8_text(
            item[0],
            f"headers[{index}].name",
            maximum_bytes=128,
            allow_empty=False,
            allow_message_controls=False,
        )
        text = _utf8_text(
            item[1],
            f"headers[{index}].value",
            maximum_bytes=8_192,
            allow_empty=True,
            allow_message_controls=False,
        )
        if _HEADER_NAME_PATTERN.fullmatch(name) is None or name in _SENSITIVE_HEADER_NAMES:
            raise ValueError("provider wire header name is forbidden")
        result.append((name, text))
    if tuple(result) != tuple(sorted(result)) or len({name for name, _ in result}) != len(result):
        raise ValueError("provider wire headers must be sorted with unique names")
    return tuple(result)


@dataclass(frozen=True, repr=False)
class NativeIMProviderRequestIntentV1:
    """One immutable, credential-free provider request plan."""

    schema_version: int
    operation: str
    method: str
    origin: CanonicalHTTPSOrigin = field(repr=False)
    path: CanonicalAbsolutePath = field(repr=False)
    query: tuple[tuple[str, str], ...] = field(repr=False)
    read_request_id: str | None
    read_request_digest: str | None = field(repr=False)
    connect_timeout_ms: int
    read_timeout_ms: int
    max_response_bytes: int
    redirect_mode: str

    _MODEL_NAME: ClassVar[str] = "NativeIMProviderRequestIntentV1"

    def __post_init__(self) -> None:
        if type(self) is not NativeIMProviderRequestIntentV1:
            raise TypeError("provider request intent requires the exact V1 class")
        _schema_version(self.schema_version)
        _enum(self.operation, {"health", "read"}, "operation")
        _enum(self.method, {"GET"}, "method")
        if type(self.origin) is not CanonicalHTTPSOrigin:
            raise TypeError("provider request origin requires the exact canonical class")
        if type(self.path) is not CanonicalAbsolutePath:
            raise TypeError("provider request path requires the exact canonical class")
        query = _query_items(self.query)
        read_request_id = _optional_id(self.read_request_id, "readRequestId")
        read_request_digest = _optional_digest(self.read_request_digest, "readRequestDigest")
        for value, label in (
            (self.connect_timeout_ms, "connectTimeoutMs"),
            (self.read_timeout_ms, "readTimeoutMs"),
            (self.max_response_bytes, "maxResponseBytes"),
        ):
            _positive_integer(value, label)
        _enum(self.redirect_mode, {"deny"}, "redirectMode")
        if self.operation == "health":
            if query or read_request_id is not None or read_request_digest is not None:
                raise ValueError("health request intent cannot carry read state")
        elif read_request_id is None or read_request_digest is None:
            raise ValueError("read request intent requires exact request binding")
        self.canonical_bytes()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "connectTimeoutMs": self.connect_timeout_ms,
            "maxResponseBytes": self.max_response_bytes,
            "method": self.method,
            "operation": self.operation,
            "origin": self.origin.canonical,
            "path": self.path.canonical,
            "query": [[name, value] for name, value in self.query],
            "readRequestDigest": self.read_request_digest,
            "readRequestId": self.read_request_id,
            "readTimeoutMs": self.read_timeout_ms,
            "redirectMode": self.redirect_mode,
            "schemaVersion": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeIMProviderRequestIntentV1:
        if cls is not NativeIMProviderRequestIntentV1:
            raise TypeError("provider request intent decoder requires the exact V1 class")
        body = _plain_dict(value, _INTENT_FIELDS, "native IM provider request intent")
        query = body["query"]
        if type(query) is not list or len(query) > _MAX_QUERY_ITEMS:
            raise TypeError("provider request intent query JSON must be a bounded plain list")
        pairs: list[tuple[str, str]] = []
        for item in query:
            if type(item) is not list or len(item) != 2:
                raise TypeError("provider request intent query JSON entries must be pairs")
            pairs.append((item[0], item[1]))
        origin = body["origin"]
        path = body["path"]
        if type(origin) is not str or type(path) is not str:
            raise TypeError("provider request intent origin/path JSON must be exact strings")
        return cls(
            schema_version=body["schemaVersion"],
            operation=body["operation"],
            method=body["method"],
            origin=CanonicalHTTPSOrigin.parse(origin),
            path=CanonicalAbsolutePath.parse(path),
            query=tuple(pairs),
            read_request_id=body["readRequestId"],
            read_request_digest=body["readRequestDigest"],
            connect_timeout_ms=body["connectTimeoutMs"],
            read_timeout_ms=body["readTimeoutMs"],
            max_response_bytes=body["maxResponseBytes"],
            redirect_mode=body["redirectMode"],
        )

    @classmethod
    def from_json_bytes(cls, encoded: object) -> NativeIMProviderRequestIntentV1:
        if cls is not NativeIMProviderRequestIntentV1:
            raise TypeError("provider request intent decoder requires the exact V1 class")
        return cls.from_dict(
            _decode_json_bytes(
                encoded,
                "native IM provider request intent",
                maximum_bytes=_MAX_INTENT_BYTES,
            )
        )

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_json_bytes(self.to_dict())
        if len(encoded) > _MAX_INTENT_BYTES:
            raise NativeIMCodecTooLargeError("provider request intent exceeds its byte limit")
        return encoded

    def canonical_digest(self) -> str:
        return _model_digest(self._MODEL_NAME, self.to_dict())

    def __repr__(self) -> str:
        return (
            "NativeIMProviderRequestIntentV1("
            f"operation={self.operation!r}, digest={self.canonical_digest()[:12]!r})"
        )


@dataclass(frozen=True, repr=False)
class NativeIMProviderWireResponseV1:
    """One bounded ephemeral response produced by an injected exchange implementation."""

    schema_version: int
    status_code: int
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    raw_body: bytes = field(repr=False)
    received_at: str
    exchange_security_evidence_digest: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self) is not NativeIMProviderWireResponseV1:
            raise TypeError("provider wire response requires the exact V1 class")
        _schema_version(self.schema_version)
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("provider wire response status must be an exact HTTP status")
        _headers(self.headers)
        if type(self.raw_body) is not bytes or len(self.raw_body) > _MAX_WIRE_BODY_BYTES:
            raise TypeError("provider wire response body must be bounded immutable bytes")
        _timestamp(self.received_at, "receivedAt")
        _digest(self.exchange_security_evidence_digest, "exchangeSecurityEvidenceDigest")

    def __repr__(self) -> str:
        return (
            "NativeIMProviderWireResponseV1("
            f"status={self.status_code}, body_bytes={len(self.raw_body)}, "
            f"evidence={self.exchange_security_evidence_digest[:12]!r})"
        )


@runtime_checkable
class NativeIMProviderExchangePortV1(Protocol):
    """Injected request executor; the provider transport itself owns no network stack."""

    async def exchange(
        self,
        intent: NativeIMProviderRequestIntentV1,
        credential: SecretMaterial,
    ) -> NativeIMProviderWireResponseV1:
        """Execute one exact intent using an opaque caller-owned credential lease."""

    async def aclose(self) -> None:
        """Release exchange-owned resources without an additional request."""


__all__ = [
    "NativeIMProviderExchangePortV1",
    "NativeIMProviderRequestIntentV1",
    "NativeIMProviderWireResponseV1",
]
