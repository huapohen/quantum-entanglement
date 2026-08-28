from __future__ import annotations

import hashlib

import pytest

from quantum_entanglement.native_im_auth import NativeIMDetachedSignatureV1
from quantum_entanglement.native_im_gateway import IMGatewayPort
from quantum_entanglement.native_im_sandbox import (
    NativeIMDisabledSandboxAdapter,
    NativeIMHealthEvidenceV1,
    NativeIMInboundRawResponseV1,
    NativeIMInboundTransportPort,
    NativeIMSandboxDisabledError,
    compose_default_native_im_sandbox_v1,
)
from quantum_entanglement.service.native_im_config import NativeIMDisabledConfigV1
from quantum_entanglement.service.secrets import SecretMaterial
from tests.test_native_im_auth import metadata_for
from tests.test_native_im_sandbox_config import bound_configuration


class PoisonedRequest:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError("disabled adapter inspected a request")

    def __repr__(self) -> str:
        raise AssertionError("disabled adapter rendered a request")


class StructuralTransport:
    async def probe_health(self, credential: SecretMaterial) -> NativeIMHealthEvidenceV1:
        raise NotImplementedError

    async def read_inbound(self, request: object, credential: SecretMaterial) -> object:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def raw_response(body: bytes = b"{}") -> NativeIMInboundRawResponseV1:
    configuration = bound_configuration()
    return NativeIMInboundRawResponseV1(
        schema_version=1,
        read_request_id="test-read-1",
        status_code=200,
        metadata=metadata_for(configuration),
        raw_body=body,
        received_at="2026-08-28T12:00:00.000001Z",
        transport_evidence_digest=hashlib.sha256(b"transport-evidence").hexdigest(),
    )


def test_default_composition_produces_only_a_disabled_gateway() -> None:
    configuration = NativeIMDisabledConfigV1(schema_version=1, enabled=False)
    adapter = compose_default_native_im_sandbox_v1(configuration)

    assert type(adapter) is NativeIMDisabledSandboxAdapter
    assert isinstance(adapter, IMGatewayPort)
    assert not hasattr(adapter, "origin")
    assert not hasattr(adapter, "credential_ref")
    assert "disabled" in repr(adapter).lower()


@pytest.mark.asyncio
async def test_every_default_gateway_method_rejects_before_request_inspection() -> None:
    adapter = compose_default_native_im_sandbox_v1(
        NativeIMDisabledConfigV1(schema_version=1, enabled=False)
    )
    poisoned = PoisonedRequest()

    for call in (
        adapter.capability_snapshot(poisoned),  # type: ignore[arg-type]
        adapter.read_inbound(poisoned),  # type: ignore[arg-type]
        adapter.dispatch(poisoned),  # type: ignore[arg-type]
        adapter.query_acceptance(poisoned),  # type: ignore[arg-type]
    ):
        with pytest.raises(NativeIMSandboxDisabledError) as raised:
            await call
        assert raised.value.code == "native_im_sandbox_disabled"
        assert raised.value.__cause__ is None

    await adapter.aclose()
    await adapter.aclose()
    assert adapter.closed is True


def test_enabled_configuration_is_not_registered_by_default_composition() -> None:
    configuration = bound_configuration()
    with pytest.raises(NativeIMSandboxDisabledError) as raised:
        compose_default_native_im_sandbox_v1(configuration)

    rendered = f"{raised.value!r} {raised.value}"
    assert configuration.origin.canonical not in rendered
    assert configuration.credential_ref.locator not in rendered
    assert configuration.verification_secret_ref.locator not in rendered


def test_default_composition_rejects_subclasses_without_reading_fields() -> None:
    class DisabledSubclass(NativeIMDisabledConfigV1):
        pass

    with pytest.raises(TypeError):
        compose_default_native_im_sandbox_v1(object.__new__(DisabledSubclass))


def test_transport_contract_is_structural_but_has_no_concrete_network_implementation() -> None:
    assert isinstance(StructuralTransport(), NativeIMInboundTransportPort)


def test_health_evidence_is_exact_bounded_and_content_free() -> None:
    evidence = NativeIMHealthEvidenceV1(
        schema_version=1,
        healthy=True,
        observed_at="2026-08-28T12:00:00.000001Z",
        evidence_digest="a" * 64,
    )
    assert evidence.healthy is True
    assert "a" * 64 not in repr(evidence)

    with pytest.raises(TypeError):
        NativeIMHealthEvidenceV1(
            schema_version=1,
            healthy=1,  # type: ignore[arg-type]
            observed_at="2026-08-28T12:00:00.000001Z",
            evidence_digest="a" * 64,
        )


def test_raw_response_is_exact_bounded_and_redacted() -> None:
    canary = b"message-body-canary"
    response = raw_response(canary)
    rendered = repr(response)

    assert response.raw_body == canary
    assert canary.decode() not in rendered
    assert response.metadata.signature not in rendered
    assert "body_bytes=19" in rendered

    for body in (bytearray(b"{}"), b"", b"x" * (16 * 1_024 * 1_024 + 1)):
        with pytest.raises((TypeError, ValueError)):
            raw_response(body)  # type: ignore[arg-type]


def test_raw_response_rejects_metadata_subclasses_and_bool_status() -> None:
    class MetadataSubclass(NativeIMDetachedSignatureV1):
        pass

    baseline = raw_response()
    with pytest.raises(TypeError):
        NativeIMInboundRawResponseV1(
            schema_version=1,
            read_request_id=baseline.read_request_id,
            status_code=200,
            metadata=object.__new__(MetadataSubclass),
            raw_body=baseline.raw_body,
            received_at=baseline.received_at,
            transport_evidence_digest=baseline.transport_evidence_digest,
        )
    with pytest.raises(ValueError):
        NativeIMInboundRawResponseV1(
            schema_version=1,
            read_request_id=baseline.read_request_id,
            status_code=True,
            metadata=baseline.metadata,
            raw_body=baseline.raw_body,
            received_at=baseline.received_at,
            transport_evidence_digest=baseline.transport_evidence_digest,
        )
