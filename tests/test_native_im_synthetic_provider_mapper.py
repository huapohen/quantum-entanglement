from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from quantum_entanglement.native_im import (
    IMCapabilityRequestV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
)
from quantum_entanglement.native_im_auth import (
    NativeIMDetachedSignatureV1,
    NativeIMRawVerificationResultV1,
)
from quantum_entanglement.native_im_provider_profile import (
    derive_inbound_only_capability_snapshot_v1,
)
from quantum_entanglement.native_im_sandbox import NativeIMInboundRawResponseV1
from tests.native_im_mapper_tck import (
    MapperTCKAcceptedV1,
    MapperTCKContextV1,
    MapperTCKRejectedV1,
    assert_native_im_mapper_tck_v1,
)
from tests.native_im_synthetic_provider_mapper import (
    MAPPER_CONTRACT_DIGEST,
    MAPPER_CONTRACT_ID,
    SyntheticSemanticProviderMapperV1,
)
from tests.test_native_im_contract import inbound_read_request
from tests.test_native_im_provider_profile import profile
from tests.test_native_im_sandbox_config import bound_configuration

NOW = "2026-08-28T12:00:00.000001Z"
TRANSPORT_EVIDENCE = hashlib.sha256(b"synthetic-provider-event-source-v1").hexdigest()


def _event(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "changeKind": "joined",
        "conversationId": "test-conversation-a",
        "correlationId": "provider-correlation-1",
        "cursor": "provider-cursor-1",
        "eventId": "provider-event-1",
        "eventType": "provider.membership.changed",
        "firstReceivedAt": "2026-08-28T11:59:00.000001Z",
        "ingressRequestId": "provider-ingress-1",
        "occurredAt": "2026-08-28T11:58:00.000001Z",
        "participant": {
            "displayName": "合成成员",
            "membershipRevision": "provider-membership-1",
            "participantId": "provider-participant-1",
            "participantKind": "human",
            "roleIds": ["provider-role-1"],
        },
        "previousMembershipRevision": None,
        "sequenceNumber": 1,
        "verificationId": "provider-verification-1",
    }
    value.update(changes)
    return value


def _payload(
    request: IMInboundReadRequestV1,
    *,
    events: list[object] | None = None,
    **changes: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "events": [_event()] if events is None else events,
        "hasMore": False,
        "readRequestId": request.read_request_id,
        "schemaVersion": 1,
        "scope": {
            "channelId": request.channel_id,
            "provider": request.provider,
            "tenantId": request.tenant_id,
            "workspaceId": request.workspace_id,
        },
        "snapshotToken": request.snapshot_token or "provider-snapshot-1",
    }
    value.update(changes)
    return value


def _raw(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _context(
    request: IMInboundReadRequestV1,
    raw_body: bytes,
    *,
    transport_evidence_digest: str = TRANSPORT_EVIDENCE,
) -> MapperTCKContextV1:
    provider_profile = profile()
    configuration = bound_configuration()
    capability = derive_inbound_only_capability_snapshot_v1(
        provider_profile,
        IMCapabilityRequestV1(
            schema_version=1,
            tenant_id=request.tenant_id,
            workspace_id=request.workspace_id,
            provider=request.provider,
            channel_id=request.channel_id,
            request_id=f"capability-{request.read_request_id}",
        ),
        observed_at=NOW,
    )
    metadata = NativeIMDetachedSignatureV1(
        schema_version=1,
        timestamp="1787908800",
        nonce=f"nonce-{request.read_request_id}",
        key_id=configuration.verification_key_id,
        signature="a" * 64,
    )
    response = NativeIMInboundRawResponseV1(
        schema_version=1,
        read_request_id=request.read_request_id,
        status_code=200,
        metadata=metadata,
        raw_body=raw_body,
        received_at=NOW,
        transport_evidence_digest=transport_evidence_digest,
    )
    verification = NativeIMRawVerificationResultV1(
        schema_version=1,
        verifier_id="test-verifier-v1",
        key_id=metadata.key_id,
        signed_at="2026-08-28T11:55:00.000001Z",
        expires_at="2026-08-28T12:05:00.000001Z",
        verified_at=NOW,
        body_digest=hashlib.sha256(raw_body).hexdigest(),
        nonce_digest=hashlib.sha256(metadata.nonce.encode()).hexdigest(),
        authentication_evidence_digest="c" * 64,
    )
    return MapperTCKContextV1(
        configuration=configuration,
        response=response,
        request=request,
        capability=capability,
        raw_verification=verification,
        profile=provider_profile,
    )


def _accepted(
    vector_id: str,
    request: IMInboundReadRequestV1,
    payload: dict[str, object],
) -> MapperTCKAcceptedV1:
    context = _context(request, _raw(payload))
    mapped = SyntheticSemanticProviderMapperV1().map_inbound(
        context.response,
        context.request,
        context.capability,
        context.raw_verification,
        context.profile,
    )
    return MapperTCKAcceptedV1(vector_id=vector_id, context=context, expected=mapped)


def _rejected(
    vector_id: str,
    request: IMInboundReadRequestV1,
    raw_body: bytes,
    code: str,
) -> MapperTCKRejectedV1:
    return MapperTCKRejectedV1(
        vector_id=vector_id,
        context=_context(request, raw_body),
        expected_error_code=code,
    )


def _vectors() -> tuple[
    tuple[MapperTCKAcceptedV1, ...],
    tuple[MapperTCKRejectedV1, ...],
]:
    initial = inbound_read_request(provider="test-provider")
    replay = inbound_read_request(
        provider="test-provider",
        read_request_id="test-read-request-replay",
    )
    continuation = inbound_read_request(
        provider="test-provider",
        read_request_id="test-read-request-continuation",
        after_cursor="provider-cursor-1",
        after_sequence=1,
        snapshot_token="provider-snapshot-1",
    )
    accepted = (
        _accepted("test-provider.accepted.initial", initial, _payload(initial)),
        _accepted("test-provider.accepted.replay", replay, _payload(replay)),
        _accepted(
            "test-provider.accepted.empty-continuation",
            continuation,
            _payload(continuation, events=[]),
        ),
    )

    duplicate_key = (
        b'{"events":[],"events":[],"hasMore":false,'
        b'"readRequestId":"test-read-request-1","schemaVersion":1,'
        b'"scope":{"channelId":"test-channel","provider":"test-provider",'
        b'"tenantId":"test-tenant","workspaceId":"test-workspace"},'
        b'"snapshotToken":"provider-snapshot-1"}'
    )
    unsupported = _payload(initial, events=[_event(eventType="provider.future.event")])
    wrong_scope = _payload(
        initial,
        scope={
            "channelId": "test-channel",
            "provider": "other-provider",
            "tenantId": "test-tenant",
            "workspaceId": "test-workspace",
        },
    )
    wrong_request = _payload(initial, readRequestId="other-read-request")
    forbidden_conversation = _payload(
        initial,
        events=[_event(conversationId="forbidden-conversation")],
    )
    limit_request = replace(initial, limit=1, read_request_id="test-read-request-limit")
    over_limit = _payload(
        limit_request,
        events=[_event(), _event(eventId="provider-event-2", sequenceNumber=2)],
    )
    rejected = (
        _rejected(
            "test-provider.rejected.duplicate-key",
            initial,
            duplicate_key,
            "native_im_mapper_payload_invalid",
        ),
        _rejected(
            "test-provider.rejected.unsupported-event",
            initial,
            _raw(unsupported),
            "native_im_mapper_payload_unsupported",
        ),
        _rejected(
            "test-provider.rejected.scope",
            initial,
            _raw(wrong_scope),
            "native_im_mapper_scope_mismatch",
        ),
        _rejected(
            "test-provider.rejected.request-correlation",
            initial,
            _raw(wrong_request),
            "native_im_mapper_correlation_mismatch",
        ),
        _rejected(
            "test-provider.rejected.conversation",
            initial,
            _raw(forbidden_conversation),
            "native_im_mapper_scope_mismatch",
        ),
        _rejected(
            "test-provider.rejected.limit",
            limit_request,
            _raw(over_limit),
            "native_im_mapper_limit_exceeded",
        ),
    )
    return accepted, rejected


def test_semantic_synthetic_provider_mapper_passes_full_offline_tck() -> None:
    accepted, rejected = _vectors()
    report = assert_native_im_mapper_tck_v1(
        SyntheticSemanticProviderMapperV1,
        SyntheticSemanticProviderMapperV1,
        mapper_contract_id=MAPPER_CONTRACT_ID,
        mapper_contract_digest=MAPPER_CONTRACT_DIGEST,
        accepted=accepted,
        rejected=rejected,
    )

    assert len(report.accepted_vector_ids) == 3
    assert len(report.rejected_vector_ids) == 6
    assert report.suite_digest == (
        "e569232b71e0989d4577604e0452b4ccb058c6b80ca981eac8334da6b34f5d51"
    )


def test_same_provider_event_is_stable_across_distinct_read_requests() -> None:
    accepted, _ = _vectors()
    initial = IMInboundPageV1.from_json_bytes(accepted[0].expected.canonical_page_body)
    replay = IMInboundPageV1.from_json_bytes(accepted[1].expected.canonical_page_body)

    assert initial.read_request_id != replay.read_request_id
    assert initial.canonical_digest() != replay.canonical_digest()
    assert initial.envelopes[0].event.canonical_bytes() == (
        replay.envelopes[0].event.canonical_bytes()
    )
    assert initial.envelopes[0].event.event_id == replay.envelopes[0].event.event_id
    assert initial.envelopes[0].event.ingress_request_id == (
        replay.envelopes[0].event.ingress_request_id
    )
    assert initial.envelopes[0].event.correlation_id == replay.envelopes[0].event.correlation_id


@pytest.mark.parametrize(
    "raw_body",
    (
        b"",
        b"\xef\xbb\xbf{}",
        b"not-json",
        b"[]",
        b'{"schemaVersion":NaN}',
    ),
)
def test_semantic_mapper_rejects_malformed_framing_without_details(raw_body: bytes) -> None:
    request = inbound_read_request(provider="test-provider")
    context = _context(request, raw_body or b"x")
    mapper = SyntheticSemanticProviderMapperV1()
    with pytest.raises(Exception) as raised:
        mapper.map_inbound(
            context.response,
            context.request,
            context.capability,
            context.raw_verification,
            context.profile,
        )
    assert raised.value.args == ("native_im_mapper_payload_invalid",)
    rendered_input = raw_body.decode("utf-8", errors="ignore")
    if rendered_input:
        assert rendered_input not in str(raised.value)
