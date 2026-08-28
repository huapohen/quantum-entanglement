from __future__ import annotations

import hashlib
import socket
from dataclasses import replace

import pytest

from quantum_entanglement.native_im import (
    IMCapabilityRequestV1,
    IMCapabilitySnapshotV1,
    IMInboundReadRequestV1,
)
from quantum_entanglement.native_im_auth import NativeIMRawVerificationResultV1
from quantum_entanglement.native_im_provider_profile import (
    IMProviderProfileV1,
    derive_inbound_only_capability_snapshot_v1,
)
from quantum_entanglement.native_im_sandbox import (
    NativeIMInboundRawResponseV1,
    NativeIMMappedPageV1,
    NativeIMMapperRejectionError,
)
from tests.native_im_mapper_tck import (
    MapperTCKAcceptedV1,
    MapperTCKContextV1,
    MapperTCKRejectedV1,
    NativeIMMapperTCKFailure,
    assert_native_im_mapper_tck_v1,
)
from tests.test_native_im_contract import inbound_read_request
from tests.test_native_im_sandbox_inbound_adapter import (
    MAPPER_CONTRACT_DIGEST,
    MAPPER_CONTRACT_ID,
    FixtureMapper,
    adapter_inputs,
)


class SyntheticTCKMapper(FixtureMapper):
    """Synthetic runner self-test only; this is not a real provider implementation."""

    def map_inbound(
        self,
        response: NativeIMInboundRawResponseV1,
        request: IMInboundReadRequestV1,
        capability: IMCapabilitySnapshotV1,
        raw_verification: object,
        profile: IMProviderProfileV1,
    ) -> NativeIMMappedPageV1:
        rejection = {
            b"reject-invalid": "native_im_mapper_payload_invalid",
            b"reject-unsupported": "native_im_mapper_payload_unsupported",
            b"reject-scope": "native_im_mapper_scope_mismatch",
            b"reject-correlation": "native_im_mapper_correlation_mismatch",
            b"reject-limit": "native_im_mapper_limit_exceeded",
        }.get(response.raw_body)
        if rejection is not None:
            raise NativeIMMapperRejectionError(rejection) from None
        return super().map_inbound(
            response,
            request,
            capability,
            raw_verification,
            profile,
        )


def _verification_for(response: NativeIMInboundRawResponseV1) -> NativeIMRawVerificationResultV1:
    return NativeIMRawVerificationResultV1(
        schema_version=1,
        verifier_id="qe.native-im.hmac-sha256.v1",
        key_id=response.metadata.key_id,
        signed_at="2026-08-28T11:55:00.000001Z",
        expires_at="2026-08-28T12:05:00.000001Z",
        verified_at="2026-08-28T12:00:00.000001Z",
        body_digest=hashlib.sha256(response.raw_body).hexdigest(),
        nonce_digest="d" * 64,
        authentication_evidence_digest="c" * 64,
    )


def _accepted_context() -> MapperTCKContextV1:
    _, request, configuration, profile, transport, _, _, _ = adapter_inputs()
    capability = derive_inbound_only_capability_snapshot_v1(
        profile,
        IMCapabilityRequestV1(
            schema_version=1,
            tenant_id=request.tenant_id,
            workspace_id=request.workspace_id,
            provider=request.provider,
            channel_id=request.channel_id,
            request_id="test-mapper-tck-capability",
        ),
        observed_at="2026-08-28T12:00:00.000001Z",
    )
    return MapperTCKContextV1(
        configuration=configuration,
        response=transport.response,
        request=request,
        capability=capability,
        raw_verification=_verification_for(transport.response),
        profile=profile,
    )


def _accepted_vector() -> MapperTCKAcceptedV1:
    context = _accepted_context()
    expected = SyntheticTCKMapper().map_inbound(
        context.response,
        context.request,
        context.capability,
        context.raw_verification,
        context.profile,
    )
    return MapperTCKAcceptedV1(
        vector_id="synthetic.accepted.membership",
        context=context,
        expected=expected,
    )


def _rejected_vector(body: bytes, code: str) -> MapperTCKRejectedV1:
    accepted = _accepted_context()
    response = replace(accepted.response, raw_body=body)
    return MapperTCKRejectedV1(
        vector_id=f"synthetic.rejected.{body.decode().removeprefix('reject-')}",
        context=replace(
            accepted,
            response=response,
            raw_verification=_verification_for(response),
        ),
        expected_error_code=code,
    )


def _rejected_vectors() -> tuple[MapperTCKRejectedV1, ...]:
    return tuple(
        _rejected_vector(body, code)
        for body, code in (
            (b"reject-invalid", "native_im_mapper_payload_invalid"),
            (b"reject-unsupported", "native_im_mapper_payload_unsupported"),
            (b"reject-scope", "native_im_mapper_scope_mismatch"),
            (b"reject-correlation", "native_im_mapper_correlation_mismatch"),
            (b"reject-limit", "native_im_mapper_limit_exceeded"),
        )
    )


def _run_tck(factory, mapper_type: type[object] = SyntheticTCKMapper):
    return assert_native_im_mapper_tck_v1(
        factory,
        mapper_type,
        mapper_contract_id=MAPPER_CONTRACT_ID,
        mapper_contract_digest=MAPPER_CONTRACT_DIGEST,
        accepted=(_accepted_vector(),),
        rejected=_rejected_vectors(),
    )


def test_mapper_tck_accepts_exact_deterministic_synthetic_subject() -> None:
    report = _run_tck(SyntheticTCKMapper)

    assert report.mapper_contract_id == MAPPER_CONTRACT_ID
    assert report.mapper_contract_digest == MAPPER_CONTRACT_DIGEST
    assert report.accepted_vector_ids == ("synthetic.accepted.membership",)
    assert report.rejected_vector_ids == (
        "synthetic.rejected.invalid",
        "synthetic.rejected.unsupported",
        "synthetic.rejected.scope",
        "synthetic.rejected.correlation",
        "synthetic.rejected.limit",
    )
    assert len(report.suite_digest) == 64
    assert "providerEvents" not in repr(report)


def test_mapper_tck_rejects_singleton_factory() -> None:
    singleton = SyntheticTCKMapper()
    with pytest.raises(NativeIMMapperTCKFailure) as raised:
        _run_tck(lambda: singleton)
    assert raised.value.code == "native_im_mapper_tck_factory_reused_instance"


def test_mapper_tck_detects_input_mutation_before_golden_comparison() -> None:
    class MutatingMapper(SyntheticTCKMapper):
        def map_inbound(self, response, request, capability, raw_verification, profile):
            object.__setattr__(request, "limit", request.limit - 1)
            return super().map_inbound(
                response,
                request,
                capability,
                raw_verification,
                profile,
            )

    with pytest.raises(NativeIMMapperTCKFailure) as raised:
        _run_tck(MutatingMapper, MutatingMapper)
    assert raised.value.code == "native_im_mapper_tck_input_mutated"


def test_mapper_tck_blocks_ambient_effects() -> None:
    class EffectMapper(SyntheticTCKMapper):
        def map_inbound(self, response, request, capability, raw_verification, profile):
            socket.socket()
            return super().map_inbound(
                response,
                request,
                capability,
                raw_verification,
                profile,
            )

    with pytest.raises(NativeIMMapperTCKFailure) as raised:
        _run_tck(EffectMapper, EffectMapper)
    assert raised.value.code == "native_im_mapper_tck_effect_forbidden"


def test_mapper_tck_rejects_subclass_output() -> None:
    class MappedSubclass(NativeIMMappedPageV1):
        pass

    class SubclassMapper(SyntheticTCKMapper):
        def map_inbound(self, response, request, capability, raw_verification, profile):
            mapped = super().map_inbound(
                response,
                request,
                capability,
                raw_verification,
                profile,
            )
            hostile = object.__new__(MappedSubclass)
            for name, value in vars(mapped).items():
                object.__setattr__(hostile, name, value)
            return hostile

    with pytest.raises(NativeIMMapperTCKFailure) as raised:
        _run_tck(SubclassMapper, SubclassMapper)
    assert raised.value.code == "native_im_mapper_tck_output_type_invalid"


def test_mapper_tck_redacts_unregistered_rejection_exception() -> None:
    canary = "provider-response-body-canary"

    class WrongRejectionMapper(SyntheticTCKMapper):
        def map_inbound(self, response, request, capability, raw_verification, profile):
            if response.raw_body.startswith(b"reject-"):
                raise RuntimeError(canary)
            return super().map_inbound(
                response,
                request,
                capability,
                raw_verification,
                profile,
            )

    with pytest.raises(NativeIMMapperTCKFailure) as raised:
        _run_tck(WrongRejectionMapper, WrongRejectionMapper)
    assert raised.value.code == "native_im_mapper_tck_rejection_type_invalid"
    assert canary not in f"{raised.value!r} {raised.value}"


def test_mapper_tck_context_rejects_cross_request_response() -> None:
    context = _accepted_context()
    wrong_request = inbound_read_request(
        provider=context.profile.provider,
        read_request_id="test-mapper-tck-wrong-request",
    )
    with pytest.raises(ValueError, match="not bound"):
        replace(context, request=wrong_request)
