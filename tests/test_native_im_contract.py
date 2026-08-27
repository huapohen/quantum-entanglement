from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from quantum_entanglement.native_im import (
    IMAttachmentRefV1,
    IMCapabilityRequestV1,
    IMConversationRefV1,
    IMMembershipChangeV1,
    IMMessageContentV1,
    IMMessageRefV1,
    IMMessageSegmentV1,
    IMParticipantRefV1,
    IMReactionRefV1,
    IMVerifiedInboundEnvelopeV1,
    InboundIMEventV1,
)

SCHEMA = 1
TIME = "2026-08-28T00:00:00.000001Z"


def conversation(**changes: object) -> IMConversationRefV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": "qe.fake-im.v1",
        "channel_id": "test-channel",
        "conversation_id": "test-conversation",
        "thread_id": None,
    }
    values.update(changes)
    return IMConversationRefV1(**values)  # type: ignore[arg-type]


def participant(**changes: object) -> IMParticipantRefV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": "qe.fake-im.v1",
        "channel_id": "test-channel",
        "participant_id": "test-human",
        "participant_kind": "human",
        "display_name": "测试成员",
        "role_ids": ("test-role-a", "test-role-b"),
        "membership_revision": "test-membership-2",
    }
    values.update(changes)
    return IMParticipantRefV1(**values)  # type: ignore[arg-type]


def attachment(**changes: object) -> IMAttachmentRefV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": "qe.fake-im.v1",
        "channel_id": "test-channel",
        "attachment_id": "test-attachment",
        "version": "test-attachment-revision-1",
        "media_type": "text/plain",
        "byte_size": 0,
        "sha256": "a" * 64,
        "immutable_ref": "test-object-1",
    }
    values.update(changes)
    return IMAttachmentRefV1(**values)  # type: ignore[arg-type]


def text_segment(text: str = "hello\nworld") -> IMMessageSegmentV1:
    return IMMessageSegmentV1(
        schema_version=SCHEMA,
        kind="text",
        text=text,
        participant_id=None,
    )


def mention_segment(participant_id: str = "test-human") -> IMMessageSegmentV1:
    return IMMessageSegmentV1(
        schema_version=SCHEMA,
        kind="mention",
        text=None,
        participant_id=participant_id,
    )


def content(**changes: object) -> IMMessageContentV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "segments": (text_segment(), mention_segment(), text_segment("done")),
        "attachments": (attachment(),),
    }
    values.update(changes)
    return IMMessageContentV1(**values)  # type: ignore[arg-type]


def message(**changes: object) -> IMMessageRefV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "conversation": conversation(),
        "message_id": "test-message",
        "revision": "test-message-revision-1",
        "created_at": TIME,
    }
    values.update(changes)
    return IMMessageRefV1(**values)  # type: ignore[arg-type]


def reaction(**changes: object) -> IMReactionRefV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": "qe.fake-im.v1",
        "channel_id": "test-channel",
        "reaction_key": "test-thumbsup",
    }
    values.update(changes)
    return IMReactionRefV1(**values)  # type: ignore[arg-type]


def inbound_event(**changes: object) -> InboundIMEventV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "event_id": "test-event-1",
        "event_type": "message.created",
        "cursor": "test-cursor-1",
        "sequence_number": 1,
        "conversation": conversation(),
        "message": message(),
        "sender": participant(),
        "content": content(),
        "reaction": None,
        "membership_change": None,
        "occurred_at": TIME,
        "first_received_at": TIME,
        "ingress_request_id": "test-ingress-request-1",
        "correlation_id": "test-correlation-1",
        "causation_id": None,
        "transport_evidence_digest": "b" * 64,
    }
    values.update(changes)
    return InboundIMEventV1(**values)  # type: ignore[arg-type]


def verified_envelope(**changes: object) -> IMVerifiedInboundEnvelopeV1:
    event = changes.pop("event", inbound_event())
    assert type(event) is InboundIMEventV1
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "event": event,
        "event_digest": event.canonical_digest(),
        "verification_id": "test-verification-1",
        "verifier_id": "test-verifier-1",
        "authentication_evidence_digest": "c" * 64,
        "tenant_mapping_revision": "test-tenant-mapping-1",
        "verified_at": TIME,
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    }
    values.update(changes)
    return IMVerifiedInboundEnvelopeV1(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        conversation(),
        participant(),
        attachment(),
        text_segment(),
        mention_segment(),
        content(),
        message(),
        reaction(),
        IMMembershipChangeV1(SCHEMA, participant(), "left", "test-membership-1"),
        inbound_event(),
        verified_envelope(),
        IMCapabilityRequestV1(
            SCHEMA,
            "test-tenant",
            "test-workspace",
            "qe.fake-im.v1",
            "test-channel",
            "test-capability-request-1",
        ),
    ],
)
def test_reference_models_round_trip_through_dict_and_noncanonical_json(value: object) -> None:
    wire = value.to_dict()  # type: ignore[attr-defined]
    encoded = json.dumps(wire, ensure_ascii=False, indent=2).encode()
    decoded = type(value).from_json_bytes(encoded)
    assert decoded == value
    assert decoded is not value
    assert decoded.canonical_bytes() == value.canonical_bytes()
    expected = hashlib.sha256(
        f"quantum-entanglement.native-im/{type(value).__name__}/1\n".encode()
        + value.canonical_bytes()
    ).hexdigest()
    assert value.canonical_digest() == expected


def test_dict_decoders_reject_unknown_missing_nonplain_and_duplicate_json_fields() -> None:
    wire = conversation().to_dict()
    for changed in (
        {**wire, "future": True},
        {key: item for key, item in wire.items() if key != "provider"},
        dict(wire, schemaVersion=True),
    ):
        with pytest.raises((TypeError, ValueError)):
            IMConversationRefV1.from_dict(changed)

    class DictSubclass(dict[str, object]):
        pass

    with pytest.raises(TypeError):
        IMConversationRefV1.from_dict(DictSubclass(wire))
    with pytest.raises(ValueError, match="duplicate"):
        IMConversationRefV1.from_json_bytes(
            b'{"schemaVersion":1,"schemaVersion":1}',
        )


def test_decoder_snapshots_nested_lists_and_to_dict_returns_fresh_plain_values() -> None:
    wire = participant().to_dict()
    decoded = IMParticipantRefV1.from_dict(wire)
    roles = wire["roleIds"]
    assert type(roles) is list
    roles.append("test-role-c")  # type: ignore[union-attr]
    wire["participantId"] = "mutated"
    assert decoded.participant_id == "test-human"
    assert decoded.role_ids == ("test-role-a", "test-role-b")
    first = decoded.to_dict()
    first["roleIds"].append("mutated")  # type: ignore[union-attr]
    assert decoded.to_dict()["roleIds"] == ["test-role-a", "test-role-b"]


def test_participant_roles_are_bounded_sorted_unique_and_authority_neutral() -> None:
    for roles in (
        ("test-role-b", "test-role-a"),
        ("test-role-a", "test-role-a"),
        ["test-role-a"],
    ):
        with pytest.raises((TypeError, ValueError)):
            participant(role_ids=roles)
    assert "测试成员" not in repr(participant())


def test_message_segment_matrix_and_content_canonicality_are_exact() -> None:
    invalid_segments = (
        lambda: IMMessageSegmentV1(SCHEMA, "text", None, None),
        lambda: IMMessageSegmentV1(SCHEMA, "text", "", None),
        lambda: IMMessageSegmentV1(SCHEMA, "text", "text", "test-human"),
        lambda: IMMessageSegmentV1(SCHEMA, "mention", "text", "test-human"),
        lambda: IMMessageSegmentV1(SCHEMA, "mention", None, None),
    )
    for factory in invalid_segments:
        with pytest.raises(ValueError):
            factory()
    with pytest.raises(ValueError, match="segment or attachment"):
        content(segments=(), attachments=())
    with pytest.raises(ValueError, match="adjacent"):
        content(segments=(text_segment("a"), text_segment("b")), attachments=())
    with pytest.raises(TypeError):
        content(segments=[text_segment()], attachments=())
    assert "hello" not in repr(content())
    assert "test-object-1" not in repr(attachment())


def test_attachment_and_message_reject_noncanonical_fields() -> None:
    for changed in (
        {"byte_size": -1},
        {"byte_size": True},
        {"sha256": "A" * 64},
        {"media_type": "text/plain; charset=utf-8"},
        {"immutable_ref": "https://example.invalid/temporary?token=secret"},
    ):
        with pytest.raises((TypeError, ValueError)):
            attachment(**changed)
    with pytest.raises(ValueError):
        message(created_at="2026-08-28T00:00:00Z")


def test_membership_change_matrix_binds_distinct_revisions() -> None:
    assert IMMembershipChangeV1(SCHEMA, participant(), "joined", None).change_kind == "joined"
    with pytest.raises(ValueError):
        IMMembershipChangeV1(SCHEMA, participant(), "joined", "test-membership-1")
    for previous in (None, "test-membership-2"):
        with pytest.raises(ValueError):
            IMMembershipChangeV1(SCHEMA, participant(), "left", previous)


def test_exact_classes_are_required_for_public_and_nested_values() -> None:
    class ConversationSubclass(IMConversationRefV1):
        pass

    with pytest.raises(TypeError, match="exact"):
        ConversationSubclass.from_dict(conversation().to_dict())
    with pytest.raises(TypeError, match="exact"):
        replace(message(), conversation=object())


@pytest.mark.parametrize(
    ("event_type", "changes"),
    [
        ("message.created", {}),
        ("message.edited", {}),
        ("message.deleted", {"content": None, "sender": None}),
        (
            "reaction.added",
            {"content": None, "reaction": reaction()},
        ),
        (
            "reaction.removed",
            {"content": None, "reaction": reaction()},
        ),
        (
            "membership.changed",
            {
                "message": None,
                "content": None,
                "reaction": None,
                "membership_change": IMMembershipChangeV1(
                    SCHEMA, participant(), "left", "test-membership-1"
                ),
            },
        ),
    ],
)
def test_inbound_event_type_matrix_accepts_only_frozen_combinations(
    event_type: str, changes: dict[str, object]
) -> None:
    assert inbound_event(event_type=event_type, **changes).event_type == event_type


def test_inbound_event_rejects_matrix_and_scope_drift() -> None:
    for changes in (
        {"content": None},
        {"reaction": reaction()},
        {"event_type": "message.deleted", "content": content()},
        {
            "message": message(conversation=conversation(channel_id="test-other-channel")),
        },
        {"sender": participant(channel_id="test-other-channel")},
        {"reaction": reaction(channel_id="test-other-channel"), "content": None},
        {"content": content(attachments=(attachment(channel_id="test-other-channel"),))},
    ):
        with pytest.raises(ValueError):
            inbound_event(**changes)


def test_verified_envelope_binds_exact_event_digest_and_traceparent() -> None:
    envelope = verified_envelope()
    decoded = IMVerifiedInboundEnvelopeV1.from_dict(envelope.to_dict())
    assert decoded == envelope
    assert "hello" not in repr(envelope)
    with pytest.raises(ValueError, match="eventDigest"):
        verified_envelope(event_digest="d" * 64)
    with pytest.raises(ValueError, match="traceparent"):
        verified_envelope(traceparent="")
    changed_event = replace(inbound_event(), sequence_number=2)
    assert changed_event.canonical_digest() != inbound_event().canonical_digest()
