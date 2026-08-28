from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from quantum_entanglement.native_im import (
    IMCapabilityRequestV1,
    IMMembershipChangeV1,
    IMVerifiedInboundEnvelopeV1,
)
from quantum_entanglement.native_im_auth import (
    NativeIMDetachedSignatureV1,
    NativeIMRawVerificationResultV1,
)
from quantum_entanglement.native_im_gateway import IMGatewayPort
from quantum_entanglement.native_im_provider_profile import (
    derive_inbound_only_capability_snapshot_v1,
)
from quantum_entanglement.native_im_sandbox import (
    NativeIMDisabledSandboxAdapter,
    NativeIMHealthEvidenceV1,
    NativeIMInboundMapperPort,
    NativeIMInboundParseError,
    NativeIMInboundRawResponseV1,
    NativeIMInboundTransportPort,
    NativeIMMappedPageV1,
    NativeIMSandboxDisabledError,
    compose_default_native_im_sandbox_v1,
    parse_native_im_inbound_page_v1,
)
from quantum_entanglement.service.native_im_config import NativeIMDisabledConfigV1
from quantum_entanglement.service.secrets import SecretMaterial
from tests.test_native_im_auth import authentication_profile, configuration_for, metadata_for
from tests.test_native_im_contract import (
    conversation,
    inbound_event,
    inbound_page,
    inbound_read_request,
    participant,
)
from tests.test_native_im_sandbox_config import bound_configuration

TRANSPORT_EVIDENCE = "b" * 64
AUTHENTICATION_EVIDENCE = "c" * 64
VERIFIED_AT = "2026-08-28T00:00:00.000001Z"


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


class StructuralMapper:
    def map_inbound(self, *values: object) -> NativeIMMappedPageV1:
        raise NotImplementedError


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


def parser_inputs():
    provider_profile = authentication_profile()
    provider_profile = replace(
        provider_profile,
        provider="qe.fake-im.v1",
        tenant_mapping_revision="test-tenant-mapping-1",
        allowed_conversation_ids=("test-conversation",),
        features=tuple(replace(item, status="supported") for item in provider_profile.features),
    )
    configuration = configuration_for(provider_profile)
    capability_request = IMCapabilityRequestV1(
        schema_version=1,
        tenant_id=provider_profile.tenant_id,
        workspace_id=provider_profile.workspace_id,
        provider=provider_profile.provider,
        channel_id=provider_profile.channel_id,
        request_id="test-capability-request-parser",
    )
    snapshot = derive_inbound_only_capability_snapshot_v1(
        provider_profile,
        capability_request,
        observed_at=VERIFIED_AT,
    )
    request = inbound_read_request(provider=provider_profile.provider)
    member = participant(provider=provider_profile.provider)
    change = IMMembershipChangeV1(
        schema_version=1,
        subject=member,
        change_kind="joined",
        previous_membership_revision=None,
    )
    event = inbound_event(
        event_type="membership.changed",
        conversation=conversation(provider=provider_profile.provider),
        message=None,
        sender=None,
        content=None,
        reaction=None,
        membership_change=change,
        transport_evidence_digest=TRANSPORT_EVIDENCE,
    )
    envelope = IMVerifiedInboundEnvelopeV1(
        schema_version=1,
        event=event,
        event_digest=event.canonical_digest(),
        verification_id="test-verification-parser-1",
        verifier_id="qe.native-im.hmac-sha256.v1",
        authentication_evidence_digest=AUTHENTICATION_EVIDENCE,
        tenant_mapping_revision=provider_profile.tenant_mapping_revision,
        verified_at=VERIFIED_AT,
        traceparent=None,
    )
    page = inbound_page(
        request=request,
        capability=snapshot,
        envelopes=(envelope,),
    )
    mapped_body = page.canonical_bytes()
    raw_body = b'{"providerEvents":["test-event-1"]}'
    verification = NativeIMRawVerificationResultV1(
        schema_version=1,
        verifier_id=envelope.verifier_id,
        key_id=configuration.verification_key_id,
        signed_at=VERIFIED_AT,
        expires_at="2026-08-28T00:05:00.000001Z",
        verified_at=VERIFIED_AT,
        body_digest=hashlib.sha256(raw_body).hexdigest(),
        nonce_digest="d" * 64,
        authentication_evidence_digest=AUTHENTICATION_EVIDENCE,
    )
    response = NativeIMInboundRawResponseV1(
        schema_version=1,
        read_request_id=request.read_request_id,
        status_code=200,
        metadata=metadata_for(configuration),
        raw_body=raw_body,
        received_at=VERIFIED_AT,
        transport_evidence_digest=TRANSPORT_EVIDENCE,
    )
    mapped = NativeIMMappedPageV1(
        schema_version=1,
        source_body_digest=verification.body_digest,
        canonical_page_body=mapped_body,
        mapping_evidence_digest="f" * 64,
    )
    return (
        response,
        mapped,
        request,
        snapshot,
        verification,
        configuration,
        provider_profile,
        page,
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
    assert isinstance(StructuralMapper(), NativeIMInboundMapperPort)


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


def test_bounded_parser_accepts_only_the_exact_canonical_bound_page() -> None:
    (
        response,
        mapped,
        request,
        snapshot,
        verification,
        configuration,
        provider_profile,
        expected,
    ) = parser_inputs()

    parsed = parse_native_im_inbound_page_v1(
        response,
        mapped,
        request,
        snapshot,
        verification,
        configuration,
        provider_profile,
    )

    assert parsed == expected
    assert parsed is not expected


def test_bounded_parser_rejects_noncanonical_json_and_body_digest_mismatch_cleanly() -> None:
    response, mapped, request, snapshot, verification, configuration, provider_profile, page = (
        parser_inputs()
    )
    pretty = json.dumps(page.to_dict(), indent=2).encode()
    noncanonical_mapping = replace(mapped, canonical_page_body=pretty)
    with pytest.raises(NativeIMInboundParseError) as noncanonical:
        parse_native_im_inbound_page_v1(
            response,
            noncanonical_mapping,
            request,
            snapshot,
            verification,
            configuration,
            provider_profile,
        )
    assert noncanonical.value.code == "native_im_parse_body_not_canonical"
    assert noncanonical.value.__cause__ is None

    canary = "signed-body-canary-must-not-render"
    with pytest.raises(NativeIMInboundParseError) as digest:
        parse_native_im_inbound_page_v1(
            response,
            mapped,
            request,
            snapshot,
            replace(verification, body_digest=hashlib.sha256(canary.encode()).hexdigest()),
            configuration,
            provider_profile,
        )
    assert digest.value.code == "native_im_parse_body_digest_mismatch"
    assert canary not in f"{digest.value!r} {digest.value}"

    malformed = b'{"message":"malformed-body-canary"'
    malformed_mapping = replace(mapped, canonical_page_body=malformed)
    with pytest.raises(NativeIMInboundParseError) as invalid:
        parse_native_im_inbound_page_v1(
            response,
            malformed_mapping,
            request,
            snapshot,
            verification,
            configuration,
            provider_profile,
        )
    assert invalid.value.code == "native_im_parse_body_invalid"
    assert invalid.value.__cause__ is None
    assert invalid.value.__context__ is None
    assert b"malformed-body-canary" not in str(invalid.value).encode()


def test_bounded_parser_intersects_request_config_profile_page_and_event_limits() -> None:
    response, mapped, request, snapshot, verification, configuration, provider_profile, page = (
        parser_inputs()
    )
    large_raw_body = b"x" * 1_025
    large_body_digest = hashlib.sha256(large_raw_body).hexdigest()
    with pytest.raises(NativeIMInboundParseError) as raw_limit:
        parse_native_im_inbound_page_v1(
            replace(response, raw_body=large_raw_body),
            replace(mapped, source_body_digest=large_body_digest),
            request,
            snapshot,
            replace(verification, body_digest=large_body_digest),
            replace(configuration, max_response_bytes=1_024),
            provider_profile,
        )
    assert raw_limit.value.code == "native_im_parse_body_too_large"

    with pytest.raises(NativeIMInboundParseError) as mapped_limit:
        parse_native_im_inbound_page_v1(
            response,
            replace(mapped, canonical_page_body=b"x" * (configuration.max_response_bytes + 1)),
            request,
            snapshot,
            verification,
            configuration,
            provider_profile,
        )
    assert mapped_limit.value.code == "native_im_parse_mapped_body_too_large"

    cases = (
        (
            replace(request, limit=configuration.page_limit + 1),
            configuration,
            provider_profile,
            "native_im_parse_request_limit_exceeded",
        ),
        (
            request,
            configuration,
            replace(
                provider_profile,
                limits=replace(
                    provider_profile.limits,
                    max_raw_event_bytes=len(page.envelopes[0].canonical_bytes()) - 1,
                ),
            ),
            "native_im_parse_event_too_large",
        ),
    )
    for changed_request, changed_configuration, changed_profile, expected_code in cases:
        with pytest.raises(NativeIMInboundParseError) as raised:
            parse_native_im_inbound_page_v1(
                response,
                mapped,
                changed_request,
                snapshot,
                verification,
                changed_configuration,
                changed_profile,
            )
        assert raised.value.code == expected_code


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("request_id", "native_im_parse_request_mismatch"),
        ("transport", "native_im_parse_transport_binding_failed"),
        ("auth", "native_im_parse_authentication_binding_failed"),
        ("mapping", "native_im_parse_tenant_mapping_mismatch"),
        ("conversation", "native_im_parse_conversation_forbidden"),
    ),
)
def test_bounded_parser_rejects_every_outer_binding_before_admission(
    mutation: str,
    expected_code: str,
) -> None:
    response, mapped, request, snapshot, verification, configuration, provider_profile, page = (
        parser_inputs()
    )
    if mutation == "request_id":
        response = replace(response, read_request_id="test-read-other")
    else:
        original = page.envelopes[0]
        event = original.event
        if mutation == "transport":
            event = replace(event, transport_evidence_digest="e" * 64)
        elif mutation == "conversation":
            changed_conversation = replace(
                event.conversation,
                conversation_id="test-conversation-forbidden",
            )
            event = replace(event, conversation=changed_conversation)
        envelope_changes: dict[str, object] = {
            "event": event,
            "event_digest": event.canonical_digest(),
        }
        if mutation == "auth":
            envelope_changes["authentication_evidence_digest"] = "e" * 64
        if mutation == "mapping":
            envelope_changes["tenant_mapping_revision"] = "test-mapping-other"
        changed_envelope = replace(original, **envelope_changes)
        page = inbound_page(request=request, capability=snapshot, envelopes=(changed_envelope,))
        mapped = replace(mapped, canonical_page_body=page.canonical_bytes())

    with pytest.raises(NativeIMInboundParseError) as raised:
        parse_native_im_inbound_page_v1(
            response,
            mapped,
            request,
            snapshot,
            verification,
            configuration,
            provider_profile,
        )
    assert raised.value.code == expected_code


def test_bounded_parser_rejects_subclasses_before_field_access() -> None:
    class ResponseSubclass(NativeIMInboundRawResponseV1):
        pass

    _, mapped, request, snapshot, verification, configuration, provider_profile, _ = parser_inputs()
    with pytest.raises(TypeError):
        parse_native_im_inbound_page_v1(
            object.__new__(ResponseSubclass),
            mapped,
            request,
            snapshot,
            verification,
            configuration,
            provider_profile,
        )
