"""Semantic synthetic provider mapper used only to prove the offline Mapper TCK."""

from __future__ import annotations

import json
from typing import Any, NoReturn

from quantum_entanglement.native_im import (
    IMCapabilitySnapshotV1,
    IMConversationRefV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
    IMMembershipChangeV1,
    IMParticipantRefV1,
    IMVerifiedInboundEnvelopeV1,
    InboundIMEventV1,
)
from quantum_entanglement.native_im_auth import NativeIMRawVerificationResultV1
from quantum_entanglement.native_im_provider_profile import IMProviderProfileV1
from quantum_entanglement.native_im_sandbox import (
    NativeIMInboundRawResponseV1,
    NativeIMMappedPageV1,
    NativeIMMapperRejectionError,
    derive_native_im_mapping_evidence_digest_v1,
)

MAPPER_CONTRACT_ID = "test-native-im-mapper-v1"
MAPPER_CONTRACT_DIGEST = "3" * 64

_ROOT_FIELDS = {
    "events",
    "hasMore",
    "readRequestId",
    "schemaVersion",
    "scope",
    "snapshotToken",
}
_SCOPE_FIELDS = {"channelId", "provider", "tenantId", "workspaceId"}
_EVENT_FIELDS = {
    "changeKind",
    "conversationId",
    "correlationId",
    "cursor",
    "eventId",
    "eventType",
    "firstReceivedAt",
    "ingressRequestId",
    "occurredAt",
    "participant",
    "previousMembershipRevision",
    "sequenceNumber",
    "verificationId",
}
_PARTICIPANT_FIELDS = {
    "displayName",
    "membershipRevision",
    "participantId",
    "participantKind",
    "roleIds",
}


def _reject(code: str) -> NoReturn:
    raise NativeIMMapperRejectionError(code) from None


def _object(value: object, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _reject("native_im_mapper_payload_invalid")
    return value


def _list(value: object) -> list[Any]:
    if type(value) is not list:
        _reject("native_im_mapper_payload_invalid")
    return value


def _decode(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        decoded: dict[str, Any] = {}
        for key, value in pairs:
            if key in decoded:
                _reject("native_im_mapper_payload_invalid")
            decoded[key] = value
        return decoded

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: _reject("native_im_mapper_payload_invalid"),
        )
    except NativeIMMapperRejectionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        _reject("native_im_mapper_payload_invalid")
    return _object(value, _ROOT_FIELDS)


class SyntheticSemanticProviderMapperV1:
    """Strict raw-page mapping candidate; never registered by production composition."""

    __slots__ = ()

    def map_inbound(
        self,
        response: NativeIMInboundRawResponseV1,
        request: IMInboundReadRequestV1,
        capability: IMCapabilitySnapshotV1,
        raw_verification: NativeIMRawVerificationResultV1,
        profile: IMProviderProfileV1,
    ) -> NativeIMMappedPageV1:
        if (
            type(response) is not NativeIMInboundRawResponseV1
            or type(request) is not IMInboundReadRequestV1
            or type(capability) is not IMCapabilitySnapshotV1
            or type(raw_verification) is not NativeIMRawVerificationResultV1
            or type(profile) is not IMProviderProfileV1
        ):
            raise TypeError("synthetic provider mapper requires exact V1 inputs")
        decoded = _decode(response.raw_body)
        if decoded["schemaVersion"] != 1:
            _reject("native_im_mapper_payload_unsupported")
        if decoded["readRequestId"] != request.read_request_id:
            _reject("native_im_mapper_correlation_mismatch")
        scope = _object(decoded["scope"], _SCOPE_FIELDS)
        expected_scope = (
            request.tenant_id,
            request.workspace_id,
            request.provider,
            request.channel_id,
        )
        if (
            (
                scope["tenantId"],
                scope["workspaceId"],
                scope["provider"],
                scope["channelId"],
            )
            != expected_scope
            or (
                profile.tenant_id,
                profile.workspace_id,
                profile.provider,
                profile.channel_id,
            )
            != expected_scope
            or (
                capability.tenant_id,
                capability.workspace_id,
                capability.provider,
                capability.channel_id,
            )
            != expected_scope
        ):
            _reject("native_im_mapper_scope_mismatch")
        event_values = _list(decoded["events"])
        maximum_events = min(request.limit, profile.limits.max_page_events)
        if len(event_values) > maximum_events:
            _reject("native_im_mapper_limit_exceeded")
        envelopes = tuple(
            self._map_event(
                value,
                response=response,
                raw_verification=raw_verification,
                profile=profile,
            )
            for value in event_values
        )
        if type(decoded["hasMore"]) is not bool:
            _reject("native_im_mapper_payload_invalid")
        try:
            page = IMInboundPageV1(
                schema_version=1,
                tenant_id=request.tenant_id,
                workspace_id=request.workspace_id,
                provider=request.provider,
                channel_id=request.channel_id,
                read_request_id=request.read_request_id,
                read_request_digest=request.canonical_digest(),
                snapshot_token=decoded["snapshotToken"],
                envelopes=envelopes,
                next_cursor=(envelopes[-1].event.cursor if envelopes else request.after_cursor),
                next_sequence=(
                    envelopes[-1].event.sequence_number if envelopes else request.after_sequence
                ),
                has_more=decoded["hasMore"],
                capability_revision=capability.revision,
                capability_digest=capability.canonical_digest(),
            )
        except (TypeError, ValueError):
            _reject("native_im_mapper_payload_invalid")
        page_digest = page.canonical_digest()
        return NativeIMMappedPageV1(
            schema_version=1,
            source_body_digest=raw_verification.body_digest,
            canonical_page_body=page.canonical_bytes(),
            mapping_evidence_digest=derive_native_im_mapping_evidence_digest_v1(
                mapper_contract_id=MAPPER_CONTRACT_ID,
                mapper_contract_digest=MAPPER_CONTRACT_DIGEST,
                profile_digest=profile.canonical_digest(),
                read_request_digest=request.canonical_digest(),
                capability_digest=capability.canonical_digest(),
                source_body_digest=raw_verification.body_digest,
                page_digest=page_digest,
            ),
        )

    def _map_event(
        self,
        value: object,
        *,
        response: NativeIMInboundRawResponseV1,
        raw_verification: NativeIMRawVerificationResultV1,
        profile: IMProviderProfileV1,
    ) -> IMVerifiedInboundEnvelopeV1:
        body = _object(value, _EVENT_FIELDS)
        mapping = next(
            (
                candidate
                for candidate in profile.event_mappings
                if candidate.provider_event_type == body["eventType"]
                and candidate.status == "supported"
            ),
            None,
        )
        if mapping is None or mapping.event_type != "membership.changed":
            _reject("native_im_mapper_payload_unsupported")
        if body["conversationId"] not in profile.allowed_conversation_ids:
            _reject("native_im_mapper_scope_mismatch")
        participant_body = _object(body["participant"], _PARTICIPANT_FIELDS)
        role_ids = _list(participant_body["roleIds"])
        if any(type(role_id) is not str for role_id in role_ids):
            _reject("native_im_mapper_payload_invalid")
        try:
            participant = IMParticipantRefV1(
                schema_version=1,
                tenant_id=profile.tenant_id,
                workspace_id=profile.workspace_id,
                provider=profile.provider,
                channel_id=profile.channel_id,
                participant_id=participant_body["participantId"],
                participant_kind=participant_body["participantKind"],
                display_name=participant_body["displayName"],
                role_ids=tuple(role_ids),
                membership_revision=participant_body["membershipRevision"],
            )
            membership = IMMembershipChangeV1(
                schema_version=1,
                subject=participant,
                change_kind=body["changeKind"],
                previous_membership_revision=body["previousMembershipRevision"],
            )
            conversation = IMConversationRefV1(
                schema_version=1,
                tenant_id=profile.tenant_id,
                workspace_id=profile.workspace_id,
                provider=profile.provider,
                channel_id=profile.channel_id,
                conversation_id=body["conversationId"],
                thread_id=None,
            )
            event = InboundIMEventV1(
                schema_version=1,
                event_id=body["eventId"],
                event_type="membership.changed",
                cursor=body["cursor"],
                sequence_number=body["sequenceNumber"],
                conversation=conversation,
                message=None,
                sender=None,
                content=None,
                reaction=None,
                membership_change=membership,
                occurred_at=body["occurredAt"],
                first_received_at=body["firstReceivedAt"],
                ingress_request_id=body["ingressRequestId"],
                correlation_id=body["correlationId"],
                causation_id=None,
                transport_evidence_digest=response.transport_evidence_digest,
            )
            return IMVerifiedInboundEnvelopeV1(
                schema_version=1,
                event=event,
                event_digest=event.canonical_digest(),
                verification_id=body["verificationId"],
                verifier_id=raw_verification.verifier_id,
                authentication_evidence_digest=(raw_verification.authentication_evidence_digest),
                tenant_mapping_revision=profile.tenant_mapping_revision,
                verified_at=raw_verification.verified_at,
                traceparent=None,
            )
        except (TypeError, ValueError):
            _reject("native_im_mapper_payload_invalid")


__all__ = [
    "MAPPER_CONTRACT_DIGEST",
    "MAPPER_CONTRACT_ID",
    "SyntheticSemanticProviderMapperV1",
]
