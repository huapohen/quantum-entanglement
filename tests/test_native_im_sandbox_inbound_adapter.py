from __future__ import annotations

import hashlib
import pickle
from dataclasses import replace
from unittest.mock import patch

import pytest

from quantum_entanglement.native_im import (
    IMAcceptanceQueryV1,
    IMCapabilityRequestV1,
    IMCapabilitySnapshotV1,
    IMDispatchRequestV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
    IMMembershipChangeV1,
    IMVerifiedInboundEnvelopeV1,
)
from quantum_entanglement.native_im_gateway import IMGatewayPort
from quantum_entanglement.native_im_provider_profile import IMProviderProfileV1
from quantum_entanglement.native_im_sandbox import (
    _APPROVED_COMPOSITION_TOKEN,
    NativeIMHealthEvidenceV1,
    NativeIMInboundOnlySandboxAdapter,
    NativeIMInboundRawResponseV1,
    NativeIMMappedPageV1,
    NativeIMMapperRejectionError,
    NativeIMOutboundForbiddenError,
    NativeIMSandboxAdapterClosedError,
    NativeIMSandboxAdapterProcessMismatchError,
    NativeIMTransportContractError,
    NativeIMVerifiedInboundReadV1,
    derive_native_im_mapping_evidence_digest_v1,
)
from quantum_entanglement.native_im_sandbox_authority import (
    NativeIMSandboxApprovalAuthorityError,
)
from quantum_entanglement.service.native_im_config import NativeIMInboundOnlyConfigV1
from quantum_entanglement.service.native_im_secrets import NativeIMSecretLoadError
from quantum_entanglement.service.secrets import SecretMaterial, SecretRef
from tests.test_native_im_auth import (
    KEY,
    NOW,
    SIGNED_UNIX_SECONDS,
    authentication_profile,
    configuration_for,
    metadata_for,
    signature_for,
)
from tests.test_native_im_contract import (
    conversation,
    inbound_event,
    inbound_page,
    inbound_read_request,
    participant,
)
from tests.test_native_im_sandbox_authority import approved_authority_for

TRANSPORT_EVIDENCE = "b" * 64
MAPPER_CONTRACT_ID = "test-native-im-mapper-v1"
MAPPER_CONTRACT_DIGEST = "3" * 64
MAPPING_EVIDENCE = "ed5536a7e07a208dab7d81d09f48a167d0b8fbc187306f9db09f65896fc304f6"
RAW_BODY = b'{"providerEvents":["test-event-1"]}'
READ_CREDENTIAL = b"test-read-only-credential"


def test_mapper_rejection_error_has_one_closed_redacted_code_catalog() -> None:
    class StrSubclass(str):
        pass

    for code in (
        "native_im_mapper_correlation_mismatch",
        "native_im_mapper_limit_exceeded",
        "native_im_mapper_payload_invalid",
        "native_im_mapper_payload_unsupported",
        "native_im_mapper_scope_mismatch",
    ):
        error = NativeIMMapperRejectionError(code)
        assert error.code == code
        assert str(error) == code
        assert repr(error) == f"NativeIMMapperRejectionError({code!r})"
        assert error.__cause__ is None
        assert error.__context__ is None

    for invalid in (
        "provider-body-canary",
        "",
        StrSubclass("native_im_mapper_payload_invalid"),
    ):
        with pytest.raises(ValueError, match="not registered"):
            NativeIMMapperRejectionError(invalid)


class ReplayGuard:
    def __init__(self) -> None:
        self.claims = 0

    def claim(self, **values: object) -> bool:
        self.claims += 1
        return True


class RecordingSecretProvider:
    def __init__(
        self,
        configuration: NativeIMInboundOnlyConfigV1,
        *,
        failure_canary: str | None = None,
    ) -> None:
        self.configuration = configuration
        self.failure_canary = failure_canary
        self.references: list[SecretRef] = []
        self.materials: list[SecretMaterial] = []

    def resolve(self, reference: SecretRef) -> SecretMaterial:
        self.references.append(reference)
        if self.failure_canary is not None:
            raise RuntimeError(self.failure_canary)
        value = KEY if reference == self.configuration.verification_secret_ref else READ_CREDENTIAL
        material = SecretMaterial(value)
        self.materials.append(material)
        return material


class FixtureTransport:
    def __init__(
        self,
        response: NativeIMInboundRawResponseV1,
        *,
        failure_canary: str | None = None,
    ) -> None:
        self.response = response
        self.failure_canary = failure_canary
        self.health_calls = 0
        self.read_calls = 0
        self.close_calls = 0

    async def probe_health(self, credential: SecretMaterial) -> NativeIMHealthEvidenceV1:
        self.health_calls += 1
        assert credential.view().tobytes() == READ_CREDENTIAL
        if self.failure_canary is not None:
            raise RuntimeError(self.failure_canary)
        return NativeIMHealthEvidenceV1(
            schema_version=1,
            healthy=True,
            observed_at=NOW,
            evidence_digest="a" * 64,
        )

    async def read_inbound(
        self,
        request: IMInboundReadRequestV1,
        credential: SecretMaterial,
    ) -> NativeIMInboundRawResponseV1:
        self.read_calls += 1
        assert credential.view().tobytes() == READ_CREDENTIAL
        if self.failure_canary is not None:
            raise RuntimeError(self.failure_canary)
        assert request.read_request_id == self.response.read_request_id
        return self.response

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.failure_canary is not None:
            raise RuntimeError(self.failure_canary)


class FixtureMapper:
    def __init__(
        self,
        *,
        failure_canary: str | None = None,
        mapping_evidence_override: str | None = None,
    ) -> None:
        self.failure_canary = failure_canary
        self.mapping_evidence_override = mapping_evidence_override
        self.calls = 0

    def map_inbound(
        self,
        response: NativeIMInboundRawResponseV1,
        request: IMInboundReadRequestV1,
        capability: IMCapabilitySnapshotV1,
        raw_verification: object,
        profile: IMProviderProfileV1,
    ) -> NativeIMMappedPageV1:
        self.calls += 1
        if self.failure_canary is not None:
            raise RuntimeError(self.failure_canary)
        verifier_id = raw_verification.verifier_id  # type: ignore[attr-defined]
        authentication_evidence = raw_verification.authentication_evidence_digest  # type: ignore[attr-defined]
        verified_at = raw_verification.verified_at  # type: ignore[attr-defined]
        source_body_digest = raw_verification.body_digest  # type: ignore[attr-defined]
        member = participant(provider=profile.provider)
        change = IMMembershipChangeV1(
            schema_version=1,
            subject=member,
            change_kind="joined",
            previous_membership_revision=None,
        )
        event = inbound_event(
            event_type="membership.changed",
            conversation=conversation(provider=profile.provider),
            message=None,
            sender=None,
            content=None,
            reaction=None,
            membership_change=change,
            transport_evidence_digest=response.transport_evidence_digest,
        )
        envelope = IMVerifiedInboundEnvelopeV1(
            schema_version=1,
            event=event,
            event_digest=event.canonical_digest(),
            verification_id="test-verification-adapter-1",
            verifier_id=verifier_id,
            authentication_evidence_digest=authentication_evidence,
            tenant_mapping_revision=profile.tenant_mapping_revision,
            verified_at=verified_at,
            traceparent=None,
        )
        page = inbound_page(
            request=request,
            capability=capability,
            envelopes=(envelope,),
        )
        evidence = derive_native_im_mapping_evidence_digest_v1(
            mapper_contract_id=MAPPER_CONTRACT_ID,
            mapper_contract_digest=MAPPER_CONTRACT_DIGEST,
            profile_digest=profile.canonical_digest(),
            read_request_digest=request.canonical_digest(),
            capability_digest=capability.canonical_digest(),
            source_body_digest=source_body_digest,
            page_digest=page.canonical_digest(),
        )
        return NativeIMMappedPageV1(
            schema_version=1,
            source_body_digest=source_body_digest,
            canonical_page_body=page.canonical_bytes(),
            mapping_evidence_digest=self.mapping_evidence_override or evidence,
        )


class PoisonedRequest:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError("outbound fence inspected a request")

    def __repr__(self) -> str:
        raise AssertionError("outbound fence rendered a request")


def provider_profile() -> IMProviderProfileV1:
    value = authentication_profile()
    return replace(
        value,
        provider="qe.fake-im.v1",
        tenant_mapping_revision="test-tenant-mapping-1",
        allowed_conversation_ids=("test-conversation",),
        features=tuple(replace(item, status="supported") for item in value.features),
    )


def adapter_inputs(
    *,
    transport_failure: str | None = None,
    mapper_failure: str | None = None,
    mapping_evidence_override: str | None = None,
    secret_failure: str | None = None,
    replay_guard: object | None = None,
):
    profile = provider_profile()
    configuration = configuration_for(profile)
    configuration, approval_authority, approval_permit, _, _ = approved_authority_for(
        configuration,
        profile,
    )
    request = inbound_read_request(provider=profile.provider)
    metadata = metadata_for(
        configuration,
        timestamp=SIGNED_UNIX_SECONDS,
        signature=signature_for(
            configuration,
            body=RAW_BODY,
            timestamp=SIGNED_UNIX_SECONDS,
        ),
    )
    response = NativeIMInboundRawResponseV1(
        schema_version=1,
        read_request_id=request.read_request_id,
        status_code=200,
        metadata=metadata,
        raw_body=RAW_BODY,
        received_at=NOW,
        transport_evidence_digest=TRANSPORT_EVIDENCE,
    )
    transport = FixtureTransport(response, failure_canary=transport_failure)
    mapper = FixtureMapper(
        failure_canary=mapper_failure,
        mapping_evidence_override=mapping_evidence_override,
    )
    secrets = RecordingSecretProvider(configuration, failure_canary=secret_failure)
    replay_guard = ReplayGuard() if replay_guard is None else replay_guard
    adapter = NativeIMInboundOnlySandboxAdapter(
        configuration,
        profile,
        approval_authority,
        approval_permit,
        "9" * 64,
        transport,
        mapper,
        secrets,
        replay_guard,
        clock=lambda: NOW,
        _composition_token=_APPROVED_COMPOSITION_TOKEN,
    )
    return adapter, request, configuration, profile, transport, mapper, secrets, replay_guard


@pytest.mark.asyncio
async def test_explicit_inbound_adapter_verifies_maps_and_returns_atomic_evidence() -> None:
    adapter, request, configuration, profile, transport, mapper, secrets, replay_guard = (
        adapter_inputs()
    )

    result = await adapter.read_verified_inbound(request)

    assert type(result) is NativeIMVerifiedInboundReadV1
    assert type(result.page) is IMInboundPageV1
    assert result.request == request
    assert result.mapping_evidence_digest == MAPPING_EVIDENCE
    assert result.raw_verification.body_digest == hashlib.sha256(RAW_BODY).hexdigest()
    assert result.page.envelopes[0].authentication_evidence_digest == (
        result.raw_verification.authentication_evidence_digest
    )
    assert result.page.envelopes[0].event.transport_evidence_digest == TRANSPORT_EVIDENCE
    assert result.capability.operations == ()
    assert result.provenance.configuration_binding_digest == (configuration.approval_binding_digest)
    assert result.provenance.profile_digest == profile.canonical_digest()
    assert result.provenance.provider_manifest_digest == "9" * 64
    assert result.provenance.read_request_digest == request.canonical_digest()
    assert result.provenance.page_digest == result.page.canonical_digest()
    assert result.provenance.transport_evidence_digest == TRANSPORT_EVIDENCE
    assert result.provenance.mapping_evidence_digest == MAPPING_EVIDENCE
    assert transport.read_calls == 1
    assert mapper.calls == 1
    assert secrets.references == [
        configuration.credential_ref,
        configuration.verification_secret_ref,
    ]
    assert all(material.closed for material in secrets.materials)
    assert replay_guard.claims == 0
    assert RAW_BODY.decode() not in repr(result)
    assert isinstance(adapter, IMGatewayPort)


def test_mapping_evidence_digest_binds_every_reviewed_input_axis() -> None:
    values = {
        "mapper_contract_id": MAPPER_CONTRACT_ID,
        "mapper_contract_digest": MAPPER_CONTRACT_DIGEST,
        "profile_digest": "4" * 64,
        "read_request_digest": "5" * 64,
        "capability_digest": "6" * 64,
        "source_body_digest": "7" * 64,
        "page_digest": "8" * 64,
    }
    baseline = derive_native_im_mapping_evidence_digest_v1(**values)
    assert len(baseline) == 64
    for field in values:
        changed = dict(values)
        changed[field] = "other-contract" if field == "mapper_contract_id" else "9" * 64
        assert derive_native_im_mapping_evidence_digest_v1(**changed) != baseline


@pytest.mark.asyncio
async def test_mapper_cannot_self_report_unbound_mapping_evidence() -> None:
    adapter, request, _, _, transport, mapper, secrets, replay_guard = adapter_inputs(
        mapping_evidence_override="f" * 64,
    )

    with pytest.raises(NativeIMTransportContractError) as raised:
        await adapter.read_verified_inbound(request)
    assert raised.value.code == "native_im_transport_contract_failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert transport.read_calls == mapper.calls == 1
    assert all(material.closed for material in secrets.materials)
    assert replay_guard.claims == 0


@pytest.mark.asyncio
async def test_revoked_before_read_rejects_before_request_secret_transport_or_mapper() -> None:
    adapter, request, _, _, transport, mapper, secrets, _ = adapter_inputs()
    authority = object.__getattribute__(
        adapter,
        "_NativeIMInboundOnlySandboxAdapter__approval_authority",
    )
    authority.revoke()

    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        await adapter.read_verified_inbound(request)
    assert raised.value.code == "native_im_sandbox_approval_revoked"
    assert secrets.references == []
    assert transport.read_calls == 0
    assert mapper.calls == 0


@pytest.mark.asyncio
async def test_revocation_during_transport_closes_credential_and_blocks_mapper_and_verifier() -> (
    None
):
    adapter, request, configuration, profile, transport, mapper, secrets, replay_guard = (
        adapter_inputs()
    )
    authority = object.__getattribute__(
        adapter,
        "_NativeIMInboundOnlySandboxAdapter__approval_authority",
    )
    permit = object.__getattribute__(
        adapter,
        "_NativeIMInboundOnlySandboxAdapter__approval_permit",
    )

    class RevokingTransport(FixtureTransport):
        async def read_inbound(self, request, credential):
            response = await super().read_inbound(request, credential)
            authority.revoke()
            return response

    revoking = RevokingTransport(transport.response)
    rebuilt = NativeIMInboundOnlySandboxAdapter(
        configuration,
        profile,
        authority,
        permit,
        "9" * 64,
        revoking,
        mapper,
        secrets,
        replay_guard,
        clock=lambda: NOW,
        _composition_token=_APPROVED_COMPOSITION_TOKEN,
    )
    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        await rebuilt.read_verified_inbound(request)
    assert raised.value.code == "native_im_sandbox_approval_revoked"
    assert revoking.read_calls == 1
    assert mapper.calls == 0
    assert secrets.references == [configuration.credential_ref]
    assert all(material.closed for material in secrets.materials)


@pytest.mark.asyncio
async def test_revocation_during_mapping_blocks_page_return_after_mapping() -> None:
    adapter, request, configuration, profile, transport, _, secrets, replay_guard = adapter_inputs()
    authority = object.__getattribute__(
        adapter,
        "_NativeIMInboundOnlySandboxAdapter__approval_authority",
    )
    permit = object.__getattribute__(
        adapter,
        "_NativeIMInboundOnlySandboxAdapter__approval_permit",
    )

    class RevokingMapper(FixtureMapper):
        def map_inbound(self, *args, **kwargs):
            mapped = super().map_inbound(*args, **kwargs)
            authority.revoke()
            return mapped

    mapper = RevokingMapper()
    rebuilt = NativeIMInboundOnlySandboxAdapter(
        configuration,
        profile,
        authority,
        permit,
        "9" * 64,
        transport,
        mapper,
        secrets,
        replay_guard,
        clock=lambda: NOW,
        _composition_token=_APPROVED_COMPOSITION_TOKEN,
    )
    with pytest.raises(NativeIMSandboxApprovalAuthorityError) as raised:
        await rebuilt.read_verified_inbound(request)
    assert raised.value.code == "native_im_sandbox_approval_revoked"
    assert mapper.calls == 1


@pytest.mark.asyncio
async def test_capability_and_health_do_not_expose_or_retain_secret_material() -> None:
    adapter, _, configuration, profile, transport, _, secrets, _ = adapter_inputs()
    capability_request = IMCapabilityRequestV1(
        schema_version=1,
        tenant_id=profile.tenant_id,
        workspace_id=profile.workspace_id,
        provider=profile.provider,
        channel_id=profile.channel_id,
        request_id="test-capability-adapter",
    )

    snapshot = await adapter.capability_snapshot(capability_request)
    assert snapshot.operations == ()
    assert secrets.references == []

    health = await adapter.probe_health()
    assert health.healthy is True
    assert secrets.references == [configuration.credential_ref]
    assert secrets.materials[0].closed is True
    assert transport.health_calls == 1


@pytest.mark.asyncio
async def test_outbound_methods_fence_before_request_clock_transport_or_secret_access() -> None:
    adapter, _, _, _, transport, mapper, secrets, _ = adapter_inputs(
        transport_failure="transport-canary",
        mapper_failure="mapper-canary",
        secret_failure="secret-canary",
    )
    poisoned = PoisonedRequest()

    for call in (
        adapter.dispatch(poisoned),  # type: ignore[arg-type]
        adapter.query_acceptance(poisoned),  # type: ignore[arg-type]
    ):
        with pytest.raises(NativeIMOutboundForbiddenError) as raised:
            await call
        assert raised.value.code == "native_im_outbound_forbidden"
        assert raised.value.__cause__ is None
    assert secrets.references == []
    assert transport.health_calls == transport.read_calls == 0
    assert mapper.calls == 0


@pytest.mark.asyncio
async def test_transport_and_mapper_failures_are_redacted_and_close_secret_leases() -> None:
    transport_canary = "transport-exception-body-canary"
    adapter, request, _, _, _, _, secrets, _ = adapter_inputs(transport_failure=transport_canary)
    with pytest.raises(NativeIMTransportContractError) as transport_error:
        await adapter.read_verified_inbound(request)
    assert transport_canary not in f"{transport_error.value!r} {transport_error.value}"
    assert transport_error.value.__cause__ is None
    assert transport_error.value.__context__ is None
    assert secrets.materials[0].closed is True

    mapper_canary = "mapper-exception-body-canary"
    adapter, request, _, _, _, _, secrets, _ = adapter_inputs(mapper_failure=mapper_canary)
    with pytest.raises(NativeIMTransportContractError) as mapper_error:
        await adapter.read_verified_inbound(request)
    assert mapper_canary not in f"{mapper_error.value!r} {mapper_error.value}"
    assert mapper_error.value.__cause__ is None
    assert mapper_error.value.__context__ is None
    assert all(material.closed for material in secrets.materials)


@pytest.mark.asyncio
async def test_hostile_secret_and_health_failures_are_redacted_and_release_leases() -> None:
    secret_canary = "secret-provider-message-body-canary"
    adapter, request, _, _, transport, mapper, secrets, _ = adapter_inputs(
        secret_failure=secret_canary
    )
    with pytest.raises(NativeIMSecretLoadError) as secret_error:
        await adapter.read_verified_inbound(request)
    assert secret_error.value.code == "native_im_secret_provider_failed"
    assert secret_canary not in f"{secret_error.value!r} {secret_error.value}"
    assert secret_error.value.__cause__ is None
    assert secret_error.value.__context__ is None
    assert transport.read_calls == 0
    assert mapper.calls == 0
    assert secrets.materials == []

    health_canary = "health-transport-secret-canary"
    adapter, _, _, _, transport, _, secrets, _ = adapter_inputs(transport_failure=health_canary)
    with pytest.raises(NativeIMTransportContractError) as health_error:
        await adapter.probe_health()
    assert health_canary not in f"{health_error.value!r} {health_error.value}"
    assert health_error.value.__cause__ is None
    assert health_error.value.__context__ is None
    assert transport.health_calls == 1
    assert len(secrets.materials) == 1
    assert secrets.materials[0].closed is True


@pytest.mark.asyncio
async def test_hostile_close_failure_is_redacted_and_successfully_retryable() -> None:
    close_canary = "close-transport-message-secret-canary"
    adapter, _, _, _, transport, _, _, _ = adapter_inputs(transport_failure=close_canary)
    with pytest.raises(NativeIMTransportContractError) as close_error:
        await adapter.aclose()
    assert close_canary not in f"{close_error.value!r} {close_error.value}"
    assert close_error.value.__cause__ is None
    assert close_error.value.__context__ is None
    assert adapter.closed is False

    transport.failure_canary = None
    await adapter.aclose()
    assert adapter.closed is True
    assert transport.close_calls == 2


@pytest.mark.asyncio
async def test_close_is_idempotent_and_closed_reads_fail_before_request_inspection() -> None:
    adapter, _, _, _, transport, _, _, _ = adapter_inputs()
    await adapter.aclose()
    await adapter.aclose()
    assert adapter.closed is True
    assert transport.close_calls == 1

    with pytest.raises(NativeIMSandboxAdapterClosedError):
        await adapter.read_inbound(PoisonedRequest())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_inbound_adapter_is_process_bound_before_request_or_component_access() -> None:
    adapter, _, _, _, transport, mapper, secrets, _ = adapter_inputs()
    with patch("quantum_entanglement.native_im_sandbox.os.getpid", return_value=1):
        with pytest.raises(NativeIMSandboxAdapterProcessMismatchError):
            await adapter.read_inbound(PoisonedRequest())  # type: ignore[arg-type]
        with pytest.raises(NativeIMSandboxAdapterProcessMismatchError):
            await adapter.probe_health()
        with pytest.raises(NativeIMSandboxAdapterProcessMismatchError):
            await adapter.aclose()
        with pytest.raises(NativeIMSandboxAdapterProcessMismatchError):
            _ = adapter.closed
        with pytest.raises(NativeIMSandboxAdapterProcessMismatchError):
            repr(adapter)
    assert transport.health_calls == transport.read_calls == transport.close_calls == 0
    assert mapper.calls == 0
    assert secrets.references == []


def test_inbound_adapter_cannot_be_serialized() -> None:
    adapter, _, _, _, _, _, _, _ = adapter_inputs()
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(adapter)


def test_inbound_adapter_constructor_rejects_subclasses_before_component_use() -> None:
    class ConfigSubclass(NativeIMInboundOnlyConfigV1):
        pass

    _, _, _, profile, transport, mapper, secrets, replay_guard = adapter_inputs()
    with pytest.raises(TypeError):
        NativeIMInboundOnlySandboxAdapter(
            object.__new__(ConfigSubclass),
            profile,
            transport,
            mapper,
            secrets,
            replay_guard,
            clock=lambda: NOW,
        )


@pytest.mark.parametrize("request_type", (IMDispatchRequestV1, IMAcceptanceQueryV1))
def test_outbound_type_names_do_not_appear_in_adapter_state(request_type: type[object]) -> None:
    adapter, _, configuration, _, _, _, _, _ = adapter_inputs()
    rendered = repr(adapter)
    assert request_type.__name__ not in rendered
    assert configuration.origin.canonical not in rendered
    assert configuration.credential_ref.locator not in rendered
