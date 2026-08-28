from __future__ import annotations

import json
from dataclasses import replace

import pytest

from quantum_entanglement.native_im_provider_exchange import (
    NativeIMProviderExchangePortV1,
    NativeIMProviderRequestIntentV1,
    NativeIMProviderWireResponseV1,
)
from quantum_entanglement.service.native_im_config import (
    CanonicalAbsolutePath,
    CanonicalHTTPSOrigin,
)
from quantum_entanglement.service.secrets import SecretMaterial
from tests.test_native_im_sandbox_config import bound_configuration

NOW = "2026-08-28T12:00:00.000001Z"


def request_intent(operation: str = "read", **changes: object) -> NativeIMProviderRequestIntentV1:
    configuration = bound_configuration()
    values: dict[str, object] = {
        "schema_version": 1,
        "operation": operation,
        "method": "GET",
        "origin": configuration.origin,
        "path": (configuration.health_path if operation == "health" else configuration.read_path),
        "query": (
            ()
            if operation == "health"
            else (
                ("limit", "100"),
                ("readRequestDigest", "a" * 64),
                ("readRequestId", "test-read-request-1"),
            )
        ),
        "read_request_id": None if operation == "health" else "test-read-request-1",
        "read_request_digest": None if operation == "health" else "a" * 64,
        "connect_timeout_ms": configuration.connect_timeout_ms,
        "read_timeout_ms": configuration.read_timeout_ms,
        "max_response_bytes": configuration.max_response_bytes,
        "redirect_mode": "deny",
    }
    values.update(changes)
    return NativeIMProviderRequestIntentV1(**values)  # type: ignore[arg-type]


def wire_response(**changes: object) -> NativeIMProviderWireResponseV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "status_code": 200,
        "headers": (
            ("content-type", "application/json"),
            ("x-native-im-key-id", "test-verification-key-1"),
        ),
        "raw_body": b'{"schemaVersion":1}',
        "received_at": NOW,
        "exchange_security_evidence_digest": "b" * 64,
    }
    values.update(changes)
    return NativeIMProviderWireResponseV1(**values)  # type: ignore[arg-type]


def test_request_intent_round_trip_is_canonical_and_credential_free() -> None:
    intent = request_intent()
    encoded = intent.canonical_bytes()

    assert NativeIMProviderRequestIntentV1.from_dict(intent.to_dict()) == intent
    assert NativeIMProviderRequestIntentV1.from_json_bytes(encoded) == intent
    assert (
        encoded
        == json.dumps(
            intent.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    assert len(intent.canonical_digest()) == 64
    assert "sandbox.im.example.com" not in repr(intent)
    assert "/v1/inbound-events" not in repr(intent)
    assert "test-read-request-1" not in repr(intent)
    assert "credential" not in intent.to_dict()


def test_health_and_read_intents_have_disjoint_state_matrices() -> None:
    health = request_intent("health")
    read = request_intent("read")

    assert health.query == ()
    assert health.read_request_id is health.read_request_digest is None
    assert read.read_request_id is not None
    assert read.read_request_digest is not None
    assert health.canonical_digest() != read.canonical_digest()

    with pytest.raises(ValueError, match="cannot carry read state"):
        request_intent(
            "health",
            query=(("readRequestId", "test-read"),),
            read_request_id="test-read",
            read_request_digest="a" * 64,
        )
    with pytest.raises(ValueError, match="requires exact request binding"):
        request_intent("read", read_request_id=None, read_request_digest=None)


@pytest.mark.parametrize(
    "query",
    (
        [("limit", "100")],
        (("readRequestId", "test-read"), ("limit", "100")),
        (("limit", "100"), ("limit", "101")),
        (("limit", "100\n"),),
        (("", "100"),),
    ),
)
def test_request_query_is_exact_sorted_unique_and_content_bounded(query: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        request_intent(query=query)


def test_request_intent_rejects_subclassed_endpoint_values_and_future_json() -> None:
    class OriginSubclass(CanonicalHTTPSOrigin):
        pass

    class PathSubclass(CanonicalAbsolutePath):
        pass

    with pytest.raises(TypeError):
        request_intent(origin=object.__new__(OriginSubclass))
    with pytest.raises(TypeError):
        request_intent(path=object.__new__(PathSubclass))

    wire = request_intent().to_dict()
    with pytest.raises(ValueError):
        NativeIMProviderRequestIntentV1.from_dict({**wire, "futureNetworkMode": "forbidden"})
    duplicate = b'{"operation":"read",' + request_intent().canonical_bytes()[1:]
    with pytest.raises(ValueError):
        NativeIMProviderRequestIntentV1.from_json_bytes(duplicate)


def test_wire_response_is_ephemeral_bounded_and_redacted() -> None:
    response = wire_response()

    assert response.status_code == 200
    assert response.raw_body == b'{"schemaVersion":1}'
    assert "schemaVersion" not in repr(response)
    assert "test-verification-key-1" not in repr(response)
    assert "application/json" not in repr(response)
    assert repr(response) == (
        "NativeIMProviderWireResponseV1(status=200, body_bytes=19, evidence='bbbbbbbbbbbb')"
    )


@pytest.mark.parametrize("status", (True, 99, 600, "200"))
def test_wire_response_rejects_non_http_status(status: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        wire_response(status_code=status)


@pytest.mark.parametrize(
    "headers",
    (
        [("content-type", "application/json")],
        (("Content-Type", "application/json"),),
        (("authorization", "secret"),),
        (("set-cookie", "secret"),),
        (("x-a", "1"), ("x-a", "2")),
        (("x-b", "1"), ("x-a", "2")),
        (("x-a", "value\r\nforged: yes"),),
    ),
)
def test_wire_response_headers_are_exact_safe_sorted_and_unique(headers: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        wire_response(headers=headers)


def test_wire_response_has_hard_immutable_body_bound() -> None:
    with pytest.raises(TypeError):
        wire_response(raw_body=bytearray(b"mutable"))
    with pytest.raises(TypeError):
        wire_response(raw_body=b"x" * (16 * 1_024 * 1_024 + 1))


@pytest.mark.asyncio
async def test_exchange_port_shape_requires_only_exchange_and_close() -> None:
    expected = wire_response()

    class FixtureExchange:
        async def exchange(self, intent, credential):
            assert type(intent) is NativeIMProviderRequestIntentV1
            assert type(credential) is SecretMaterial
            return expected

        async def aclose(self):
            return None

    exchange = FixtureExchange()
    assert isinstance(exchange, NativeIMProviderExchangePortV1)
    credential = SecretMaterial(b"synthetic-test-only")
    try:
        assert await exchange.exchange(request_intent(), credential) is expected
    finally:
        credential.close()
    await exchange.aclose()
    assert not hasattr(exchange, "dispatch")
    assert not hasattr(exchange, "query_acceptance")


def test_intent_digest_changes_on_every_request_axis() -> None:
    baseline = request_intent()
    variants = (
        replace(baseline, path=CanonicalAbsolutePath("/v1/other")),
        replace(baseline, query=(("limit", "99"),)),
        replace(baseline, read_request_id="test-read-request-2"),
        replace(baseline, read_request_digest="c" * 64),
        replace(baseline, connect_timeout_ms=baseline.connect_timeout_ms + 1),
        replace(baseline, read_timeout_ms=baseline.read_timeout_ms + 1),
        replace(baseline, max_response_bytes=baseline.max_response_bytes - 1),
    )
    assert all(variant.canonical_digest() != baseline.canonical_digest() for variant in variants)
