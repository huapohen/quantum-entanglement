from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from quantum_entanglement.native_im import IMInboundReadRequestV1
from quantum_entanglement.native_im_provider_exchange import (
    NativeIMProviderExchangePortV1,
    NativeIMProviderWireResponseV1,
)
from quantum_entanglement.native_im_sandbox import (
    NativeIMHealthEvidenceV1,
    NativeIMInboundRawExchangeV1,
    NativeIMInboundRawResponseV1,
    NativeIMTransportContractError,
    derive_native_im_health_evidence_digest_v1,
)
from quantum_entanglement.service.secrets import SecretMaterial
from tests.native_im_mapper_tck import native_im_mapper_zero_effect_fence_v1
from tests.native_im_synthetic_provider_transport import (
    HEALTH_BODY,
    TRANSPORT_CONTRACT_DIGEST,
    TRANSPORT_CONTRACT_ID,
    ScriptedNativeIMExchangeStepV1,
    ScriptedNativeIMProviderExchangeV1,
    SyntheticSemanticProviderTransportV1,
)
from tests.test_native_im_contract import inbound_read_request
from tests.test_native_im_provider_profile import profile
from tests.test_native_im_sandbox_composition import manifest_for
from tests.test_native_im_sandbox_config import bound_configuration

NOW = "2026-08-28T12:00:00.000001Z"
RAW_BODY = b'{"events":[],"schemaVersion":1}'
EXCHANGE_EVIDENCE = "e" * 64
EVENT_SOURCE_EVIDENCE = "b" * 64


class PlanningExchange:
    async def exchange(self, intent, credential):
        raise AssertionError("planning exchange cannot execute")

    async def aclose(self):
        return None


def _base_transport(
    exchange: NativeIMProviderExchangePortV1 | None = None,
) -> SyntheticSemanticProviderTransportV1:
    provider_profile = profile()
    manifest = manifest_for(provider_profile)
    return SyntheticSemanticProviderTransportV1(
        bound_configuration(),
        provider_profile,
        manifest.canonical_digest(),
        PlanningExchange() if exchange is None else exchange,
        clock=lambda: NOW,
    )


def _health_response(**changes: object) -> NativeIMProviderWireResponseV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "status_code": 200,
        "headers": (("content-type", "application/json"),),
        "raw_body": HEALTH_BODY,
        "received_at": NOW,
        "exchange_security_evidence_digest": EXCHANGE_EVIDENCE,
    }
    values.update(changes)
    return NativeIMProviderWireResponseV1(**values)  # type: ignore[arg-type]


def _read_headers(request: IMInboundReadRequestV1, **changes: str) -> tuple[tuple[str, str], ...]:
    values = {
        "x-native-im-event-source-digest": EVENT_SOURCE_EVIDENCE,
        "x-native-im-key-id": "test-verification-key-1",
        "x-native-im-nonce": f"nonce-{request.read_request_id}",
        "x-native-im-read-request-id": request.read_request_id,
        "x-native-im-signature": "a" * 64,
        "x-native-im-timestamp": "1787908800",
    }
    values.update(changes)
    return tuple(sorted(values.items()))


def _read_response(
    request: IMInboundReadRequestV1,
    **changes: object,
) -> NativeIMProviderWireResponseV1:
    values: dict[str, object] = {
        "schema_version": 1,
        "status_code": 200,
        "headers": _read_headers(request),
        "raw_body": RAW_BODY,
        "received_at": NOW,
        "exchange_security_evidence_digest": EXCHANGE_EVIDENCE,
    }
    values.update(changes)
    return NativeIMProviderWireResponseV1(**values)  # type: ignore[arg-type]


def _scripted_transport(
    steps: tuple[ScriptedNativeIMExchangeStepV1, ...],
) -> tuple[SyntheticSemanticProviderTransportV1, ScriptedNativeIMProviderExchangeV1]:
    exchange = ScriptedNativeIMProviderExchangeV1(steps)
    return _base_transport(exchange), exchange


@pytest.mark.asyncio
async def test_transport_tck_health_is_exact_bound_and_zero_effect() -> None:
    intent = _base_transport().health_intent()
    transport, exchange = _scripted_transport(
        (ScriptedNativeIMExchangeStepV1(intent, response=_health_response()),)
    )
    credential = SecretMaterial(b"synthetic-health-credential-canary")
    try:
        with (
            native_im_mapper_zero_effect_fence_v1(),
            patch.object(
                SecretMaterial,
                "view",
                side_effect=AssertionError("transport inspected credential"),
            ),
        ):
            evidence = await transport.probe_health(credential)
    finally:
        credential.close()

    assert type(evidence) is NativeIMHealthEvidenceV1
    assert evidence.request_intent_digest == intent.canonical_digest()
    assert evidence.exchange_security_evidence_digest == EXCHANGE_EVIDENCE
    assert evidence.transport_contract_id == TRANSPORT_CONTRACT_ID
    assert evidence.transport_contract_digest == TRANSPORT_CONTRACT_DIGEST
    assert evidence.evidence_digest == derive_native_im_health_evidence_digest_v1(
        healthy=evidence.healthy,
        observed_at=evidence.observed_at,
        status_code=evidence.status_code,
        configuration_binding_digest=evidence.configuration_binding_digest,
        profile_digest=evidence.profile_digest,
        provider_manifest_digest=evidence.provider_manifest_digest,
        transport_contract_id=evidence.transport_contract_id,
        transport_contract_digest=evidence.transport_contract_digest,
        request_intent_digest=evidence.request_intent_digest,
        exchange_security_evidence_digest=evidence.exchange_security_evidence_digest,
    )
    assert exchange.consumed
    assert exchange.credential_ids == [id(credential)]


@pytest.mark.asyncio
async def test_transport_tck_initial_read_builds_exact_intent_without_reading_credential() -> None:
    request = inbound_read_request(provider="test-provider")
    intent = _base_transport().read_intent(request)
    transport, exchange = _scripted_transport(
        (ScriptedNativeIMExchangeStepV1(intent, response=_read_response(request)),)
    )
    credential = SecretMaterial(b"synthetic-read-credential-canary")
    try:
        with (
            native_im_mapper_zero_effect_fence_v1(),
            patch.object(
                SecretMaterial,
                "view",
                side_effect=AssertionError("transport inspected credential"),
            ),
        ):
            response = await transport.read_inbound(request, credential)
    finally:
        credential.close()

    assert type(response) is NativeIMInboundRawResponseV1
    assert response.read_request_id == request.read_request_id
    assert response.raw_body == RAW_BODY
    assert response.received_at == NOW
    assert response.transport_evidence_digest == EVENT_SOURCE_EVIDENCE
    assert intent.query == (
        ("limit", "100"),
        ("readRequestDigest", request.canonical_digest()),
        ("readRequestId", request.read_request_id),
    )
    assert exchange.consumed


@pytest.mark.asyncio
async def test_transport_tck_enhanced_read_binds_transient_exchange_evidence() -> None:
    request = inbound_read_request(provider="test-provider")
    intent = _base_transport().read_intent(request)
    transport, exchange = _scripted_transport(
        (ScriptedNativeIMExchangeStepV1(intent, response=_read_response(request)),)
    )
    credential = SecretMaterial(b"synthetic-enhanced-read-credential")
    try:
        observed = await transport.read_inbound_exchange(request, credential)
    finally:
        credential.close()

    assert type(observed) is NativeIMInboundRawExchangeV1
    observed.exchange_evidence.validate_request_binding(request)
    assert observed.exchange_evidence.request_intent_digest == intent.canonical_digest()
    assert observed.exchange_evidence.exchange_security_evidence_digest == EXCHANGE_EVIDENCE
    assert (
        observed.exchange_evidence.event_source_evidence_digest
        == EVENT_SOURCE_EVIDENCE
        == observed.response.transport_evidence_digest
    )
    assert exchange.consumed


@pytest.mark.asyncio
async def test_transport_tck_repeat_event_source_does_not_reuse_exchange_evidence() -> None:
    request = inbound_read_request(provider="test-provider")
    intent = _base_transport().read_intent(request)
    first_transport, _ = _scripted_transport(
        (
            ScriptedNativeIMExchangeStepV1(
                intent,
                response=_read_response(
                    request,
                    received_at=NOW,
                    exchange_security_evidence_digest="1" * 64,
                ),
            ),
        )
    )
    second_transport = SyntheticSemanticProviderTransportV1(
        bound_configuration(),
        profile(),
        manifest_for(profile()).canonical_digest(),
        ScriptedNativeIMProviderExchangeV1(
            (
                ScriptedNativeIMExchangeStepV1(
                    intent,
                    response=_read_response(
                        request,
                        received_at="2026-08-28T12:00:01.000001Z",
                        exchange_security_evidence_digest="2" * 64,
                    ),
                ),
            )
        ),
        clock=lambda: "2026-08-28T12:00:01.000001Z",
    )
    first_credential = SecretMaterial(b"synthetic-repeat-first")
    second_credential = SecretMaterial(b"synthetic-repeat-second")
    try:
        first = await first_transport.read_inbound_exchange(request, first_credential)
        second = await second_transport.read_inbound_exchange(request, second_credential)
    finally:
        first_credential.close()
        second_credential.close()

    assert first.response.raw_body == second.response.raw_body
    assert (
        first.response.transport_evidence_digest
        == second.response.transport_evidence_digest
        == EVENT_SOURCE_EVIDENCE
    )
    assert first.exchange_evidence.evidence_digest != second.exchange_evidence.evidence_digest


def test_transport_tck_continuation_intent_binds_cursor_sequence_snapshot_and_request() -> None:
    request = inbound_read_request(
        provider="test-provider",
        read_request_id="test-read-continuation",
        after_cursor="provider-cursor-1",
        after_sequence=1,
        snapshot_token="provider-snapshot-1",
        limit=25,
    )
    intent = _base_transport().read_intent(request)

    assert intent.read_request_id == request.read_request_id
    assert intent.read_request_digest == request.canonical_digest()
    assert intent.query == (
        ("afterCursor", "provider-cursor-1"),
        ("afterSequence", "1"),
        ("limit", "25"),
        ("readRequestDigest", request.canonical_digest()),
        ("readRequestId", "test-read-continuation"),
        ("snapshotToken", "provider-snapshot-1"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("disconnect", "timeout"))
async def test_transport_tck_redacts_scripted_exchange_faults(fault: str) -> None:
    request = inbound_read_request(provider="test-provider")
    intent = _base_transport().read_intent(request)
    transport, exchange = _scripted_transport(
        (ScriptedNativeIMExchangeStepV1(intent, fault_code=fault),)
    )
    credential = SecretMaterial(b"synthetic-fault-credential")
    try:
        with pytest.raises(NativeIMTransportContractError) as raised:
            await transport.read_inbound(request, credential)
    finally:
        credential.close()
    assert raised.value.code == "native_im_transport_contract_failed"
    assert "provider-content-canary" not in f"{raised.value!r} {raised.value}"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert exchange.consumed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (204, 206, 301, 401, 403, 404, 429, 500))
async def test_transport_tck_rejects_every_non_200_read_status(status: int) -> None:
    request = inbound_read_request(provider="test-provider")
    intent = _base_transport().read_intent(request)
    transport, _ = _scripted_transport(
        (
            ScriptedNativeIMExchangeStepV1(
                intent,
                response=_read_response(request, status_code=status),
            ),
        )
    )
    credential = SecretMaterial(b"synthetic-status-credential")
    try:
        with pytest.raises(NativeIMTransportContractError):
            await transport.read_inbound(request, credential)
    finally:
        credential.close()


@pytest.mark.asyncio
async def test_transport_tck_rejects_cross_request_signed_response() -> None:
    request = inbound_read_request(provider="test-provider")
    intent = _base_transport().read_intent(request)
    wrong_headers = _read_headers(
        request,
        **{"x-native-im-read-request-id": "other-read-request"},
    )
    transport, _ = _scripted_transport(
        (
            ScriptedNativeIMExchangeStepV1(
                intent,
                response=_read_response(request, headers=wrong_headers),
            ),
        )
    )
    credential = SecretMaterial(b"synthetic-correlation-credential")
    try:
        with pytest.raises(NativeIMTransportContractError):
            await transport.read_inbound(request, credential)
    finally:
        credential.close()


@pytest.mark.asyncio
async def test_transport_tck_rejects_missing_or_extra_signed_headers() -> None:
    request = inbound_read_request(provider="test-provider")
    intent = _base_transport().read_intent(request)
    missing = tuple(item for item in _read_headers(request) if item[0] != "x-native-im-signature")
    transport, _ = _scripted_transport(
        (
            ScriptedNativeIMExchangeStepV1(
                intent,
                response=_read_response(request, headers=missing),
            ),
        )
    )
    credential = SecretMaterial(b"synthetic-header-credential")
    try:
        with pytest.raises(NativeIMTransportContractError):
            await transport.read_inbound(request, credential)
    finally:
        credential.close()


@pytest.mark.asyncio
async def test_transport_tck_close_is_idempotent_and_blocks_new_exchange() -> None:
    intent = _base_transport().health_intent()
    transport, exchange = _scripted_transport(
        (ScriptedNativeIMExchangeStepV1(intent, response=_health_response()),)
    )
    await transport.aclose()
    await transport.aclose()
    assert transport.closed
    assert exchange.closed
    assert not exchange.consumed

    credential = SecretMaterial(b"synthetic-closed-credential")
    try:
        with pytest.raises(NativeIMTransportContractError):
            await transport.probe_health(credential)
    finally:
        credential.close()


def test_scripted_exchange_exposes_no_endpoint_or_outbound_surface() -> None:
    intent = _base_transport().health_intent()
    exchange = ScriptedNativeIMProviderExchangeV1(
        (ScriptedNativeIMExchangeStepV1(intent, response=_health_response()),)
    )

    assert not hasattr(exchange, "endpoint")
    assert not hasattr(exchange, "dispatch")
    assert not hasattr(exchange, "query_acceptance")
    assert "sandbox.im.example.com" not in repr(exchange)


def test_scripted_exchange_step_requires_one_exact_outcome() -> None:
    intent = _base_transport().health_intent()
    with pytest.raises(ValueError, match="exactly one outcome"):
        ScriptedNativeIMExchangeStepV1(intent)
    with pytest.raises(ValueError, match="exactly one outcome"):
        ScriptedNativeIMExchangeStepV1(
            intent,
            response=_health_response(),
            fault_code="disconnect",
        )
    with pytest.raises(ValueError, match="not registered"):
        ScriptedNativeIMExchangeStepV1(intent, fault_code="provider-canary")


def test_transport_intent_changes_with_every_approved_configuration_axis() -> None:
    baseline = _base_transport().health_intent()
    configuration = bound_configuration()
    provider_profile = profile()
    manifest = manifest_for(provider_profile)
    changed_configuration = replace(
        configuration,
        connect_timeout_ms=configuration.connect_timeout_ms + 1,
    )
    changed = SyntheticSemanticProviderTransportV1(
        changed_configuration,
        provider_profile,
        manifest.canonical_digest(),
        PlanningExchange(),
        clock=lambda: NOW,
    ).health_intent()
    assert changed.canonical_digest() != baseline.canonical_digest()
