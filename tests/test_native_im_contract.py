from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from quantum_entanglement.native_im import (
    IMAcceptanceLookupCapabilityV1,
    IMActionCommandV1,
    IMActionIntentV1,
    IMAttachmentRefV1,
    IMCapabilityRequestV1,
    IMCapabilitySnapshotV1,
    IMConversationRefV1,
    IMDispatchRequestV1,
    IMInboundPageV1,
    IMInboundReadRequestV1,
    IMMembershipChangeV1,
    IMMessageContentV1,
    IMMessageRefV1,
    IMMessageSegmentV1,
    IMOperationCapabilityV1,
    IMParticipantRefV1,
    IMReactionRefV1,
    IMVerifiedInboundEnvelopeV1,
    InboundIMEventV1,
    derive_im_idempotency_key_v1,
)

SCHEMA = 1
TIME = "2026-08-28T00:00:00.000001Z"
LATER_TIME = "2026-08-28T00:00:01.000001Z"


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


def lookup(
    lookup_mode: str = "idempotency_key", **changes: object
) -> IMAcceptanceLookupCapabilityV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "lookup_mode": lookup_mode,
        "negative_acceptance_mode": "authoritative_terminal",
        "retention_seconds": 3_600,
        "consistency_seconds": 5,
    }
    values.update(changes)
    return IMAcceptanceLookupCapabilityV1(**values)  # type: ignore[arg-type]


def operation(name: str = "send_message", **changes: object) -> IMOperationCapabilityV1:
    no_revision = {"send_message", "add_reaction", "remove_reaction"}
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "operation": name,
        "revision_mode": "not_applicable" if name in no_revision else "required_cas",
        "idempotency_mode": "receiver_deduplicated",
        "acceptance_lookups": (
            lookup("idempotency_key"),
            lookup("provider_operation_id"),
        ),
    }
    values.update(changes)
    return IMOperationCapabilityV1(**values)  # type: ignore[arg-type]


def capability(**changes: object) -> IMCapabilitySnapshotV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": "qe.fake-im.v1",
        "channel_id": "test-channel",
        "revision": "test-capability-1",
        "observed_at": TIME,
        "operations": (operation("send_message"),),
        "idempotency_retention_seconds": 3_600,
        "supports_threads": True,
        "supports_mentions": True,
        "supports_attachments": True,
        "supports_membership_events": True,
        "max_text_bytes": 1_024,
        "max_attachments": 4,
        "max_attachment_bytes": 1_048_576,
    }
    values.update(changes)
    return IMCapabilitySnapshotV1(**values)  # type: ignore[arg-type]


def inbound_read_request(**changes: object) -> IMInboundReadRequestV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": "qe.fake-im.v1",
        "channel_id": "test-channel",
        "after_cursor": None,
        "after_sequence": None,
        "snapshot_token": None,
        "limit": 100,
        "read_request_id": "test-read-request-1",
    }
    values.update(changes)
    return IMInboundReadRequestV1(**values)  # type: ignore[arg-type]


def inbound_page(**changes: object) -> IMInboundPageV1:
    request = changes.pop("request", inbound_read_request())
    snapshot = changes.pop("capability", capability())
    envelopes = changes.pop("envelopes", (verified_envelope(),))
    assert type(request) is IMInboundReadRequestV1
    assert type(snapshot) is IMCapabilitySnapshotV1
    assert type(envelopes) is tuple
    final_event = envelopes[-1].event if envelopes else None
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "tenant_id": request.tenant_id,
        "workspace_id": request.workspace_id,
        "provider": request.provider,
        "channel_id": request.channel_id,
        "read_request_id": request.read_request_id,
        "read_request_digest": request.canonical_digest(),
        "snapshot_token": request.snapshot_token or "test-snapshot-1",
        "envelopes": envelopes,
        "next_cursor": final_event.cursor if final_event is not None else request.after_cursor,
        "next_sequence": (
            final_event.sequence_number if final_event is not None else request.after_sequence
        ),
        "has_more": bool(envelopes),
        "capability_revision": snapshot.revision,
        "capability_digest": snapshot.canonical_digest(),
    }
    values.update(changes)
    return IMInboundPageV1(**values)  # type: ignore[arg-type]


def action_intent(**changes: object) -> IMActionIntentV1:
    operation_name = changes.get("operation", "send_message")
    target: IMMessageRefV1 | None = None
    body: IMMessageContentV1 | None = content()
    reaction_value: IMReactionRefV1 | None = None
    if operation_name == "edit_message":
        target = message()
    elif operation_name == "delete_message":
        target = message()
        body = None
    elif operation_name in {"add_reaction", "remove_reaction"}:
        target = message()
        body = None
        reaction_value = reaction()
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "action_id": "test-action-1",
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "actor_id": "test-actor-1",
        "delegator_id": None,
        "conversation": conversation(),
        "operation": operation_name,
        "target_message": target,
        "content": body,
        "reaction": reaction_value,
        "created_at": TIME,
        "correlation_id": "test-correlation-1",
        "causation_id": "test-causation-1",
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    }
    values.update(changes)
    return IMActionIntentV1(**values)  # type: ignore[arg-type]


def action_command(**changes: object) -> IMActionCommandV1:
    intent = changes.pop("intent", action_intent())
    snapshot = changes.pop("capability", capability())
    assert type(intent) is IMActionIntentV1
    assert type(snapshot) is IMCapabilitySnapshotV1
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "command_id": "test-command-1",
        "intent": intent,
        "intent_digest": intent.canonical_digest(),
        "idempotency_key": derive_im_idempotency_key_v1(intent),
        "authorization_decision_id": "test-authorization-decision-1",
        "authorization_revision": "test-authorization-revision-1",
        "approval_decision_id": None,
        "approval_revision": None,
        "policy_revision": "test-policy-revision-1",
        "capability_revision": snapshot.revision,
        "capability_digest": snapshot.canonical_digest(),
        "authorized_at": TIME,
        "expires_at": LATER_TIME,
        "correlation_id": intent.correlation_id,
        "causation_id": intent.action_id,
        "traceparent": intent.traceparent,
    }
    values.update(changes)
    return IMActionCommandV1(**values)  # type: ignore[arg-type]


def dispatch_request(**changes: object) -> IMDispatchRequestV1:
    command = changes.pop("command", action_command())
    assert type(command) is IMActionCommandV1
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "dispatch_attempt_id": "test-dispatch-attempt-1",
        "command": command,
        "command_digest": command.canonical_digest(),
        "attempt_number": 1,
        "fence_id": "test-fence-1",
        "fence_revision": "test-fence-revision-1",
        "claimed_at": TIME,
        "dispatch_deadline_at": command.expires_at,
        "correlation_id": command.correlation_id,
        "causation_id": command.command_id,
        "traceparent": command.traceparent,
    }
    values.update(changes)
    return IMDispatchRequestV1(**values)  # type: ignore[arg-type]


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
        lookup(),
        operation(),
        capability(),
        inbound_read_request(),
        inbound_read_request(
            after_cursor="test-cursor-1",
            after_sequence=1,
            snapshot_token="test-snapshot-1",
        ),
        inbound_page(),
        action_intent(),
        action_intent(operation="edit_message", delegator_id="test-delegator-1"),
        action_command(),
        action_command(
            approval_decision_id="test-approval-decision-1",
            approval_revision="test-approval-revision-1",
        ),
        dispatch_request(),
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


def test_acceptance_lookup_windows_are_exact_signed_64_bit_values() -> None:
    assert lookup().consistency_seconds < lookup().retention_seconds
    for changes in (
        {"retention_seconds": 0},
        {"retention_seconds": True},
        {"consistency_seconds": -1},
        {"consistency_seconds": 3_600},
        {"lookup_mode": "future"},
        {"negative_acceptance_mode": "eventually_probable"},
    ):
        with pytest.raises((TypeError, ValueError)):
            lookup(**changes)


@pytest.mark.parametrize(
    ("name", "revision_mode"),
    [
        ("send_message", "not_applicable"),
        ("add_reaction", "not_applicable"),
        ("remove_reaction", "not_applicable"),
        ("edit_message", "required_cas"),
        ("delete_message", "provider_best_effort"),
    ],
)
def test_operation_revision_matrix(name: str, revision_mode: str) -> None:
    assert operation(name, revision_mode=revision_mode).revision_mode == revision_mode


def test_operation_capability_rejects_order_duplicates_and_impossible_guarantees() -> None:
    idempotency = lookup("idempotency_key")
    provider = lookup("provider_operation_id")
    for changes in (
        {"acceptance_lookups": (provider, idempotency)},
        {"acceptance_lookups": (idempotency, idempotency)},
        {
            "idempotency_mode": "not_supported",
            "acceptance_lookups": (idempotency,),
        },
        {"revision_mode": "required_cas"},
    ):
        with pytest.raises(ValueError):
            operation(**changes)
    with pytest.raises(ValueError):
        operation("edit_message", revision_mode="not_applicable")
    no_dedup = operation(
        idempotency_mode="not_supported",
        acceptance_lookups=(provider,),
    )
    assert no_dedup.idempotency_mode == "not_supported"


def test_capability_snapshot_binds_sorted_operations_and_retention() -> None:
    add_reaction = operation("add_reaction")
    send = operation("send_message")
    assert capability(operations=(add_reaction, send)).operations == (add_reaction, send)
    for changes in (
        {"operations": (send, add_reaction)},
        {"operations": (send, send)},
        {"idempotency_retention_seconds": None},
        {"supports_threads": 1},
        {"max_text_bytes": True},
        {"max_attachments": -1},
    ):
        with pytest.raises((TypeError, ValueError)):
            capability(**changes)
    no_dedup_operation = operation(
        idempotency_mode="not_supported",
        acceptance_lookups=(lookup("provider_operation_id"),),
    )
    assert (
        capability(
            operations=(no_dedup_operation,),
            idempotency_retention_seconds=None,
        ).idempotency_retention_seconds
        is None
    )
    with pytest.raises(ValueError):
        capability(
            operations=(no_dedup_operation,),
            idempotency_retention_seconds=10,
        )


def test_inbound_read_request_requires_exact_resume_pair_and_limit() -> None:
    continuation = inbound_read_request(
        after_cursor="test-cursor-1",
        after_sequence=1,
        snapshot_token="test-snapshot-1",
        limit=1_000,
    )
    assert continuation.after_cursor == "test-cursor-1"
    assert continuation.after_sequence == 1
    for changes in (
        {"after_cursor": "test-cursor-1"},
        {"after_sequence": 1},
        {"snapshot_token": "test-snapshot-1"},
        {"limit": 0},
        {"limit": 1_001},
        {"limit": True},
    ):
        with pytest.raises((TypeError, ValueError)):
            inbound_read_request(**changes)

    assert (
        inbound_read_request(
            after_cursor="test-stable-cursor-1",
            after_sequence=1,
            snapshot_token=None,
        ).snapshot_token
        is None
    )


def test_inbound_page_round_trip_preserves_immutable_nested_values() -> None:
    first = verified_envelope()
    second = verified_envelope(
        event=inbound_event(
            event_id="test-event-2",
            cursor="test-cursor-2",
            sequence_number=2,
        ),
        verification_id="test-verification-2",
    )
    page = inbound_page(envelopes=(first, second), has_more=False)
    decoded = IMInboundPageV1.from_json_bytes(
        json.dumps(page.to_dict(), ensure_ascii=False, indent=2).encode()
    )
    assert decoded == page
    assert decoded.envelopes == (first, second)
    assert type(decoded.envelopes) is tuple
    assert decoded.next_cursor == "test-cursor-2"
    assert decoded.next_sequence == 2


def test_inbound_page_rejects_scope_sequence_event_and_next_pair_drift() -> None:
    first = verified_envelope()
    second = verified_envelope(
        event=inbound_event(
            event_id="test-event-2",
            cursor="test-cursor-2",
            sequence_number=2,
        ),
        verification_id="test-verification-2",
    )
    duplicate_sequence = verified_envelope(
        event=inbound_event(event_id="test-event-2", cursor="test-cursor-2"),
        verification_id="test-verification-2",
    )
    duplicate_event = verified_envelope(
        event=inbound_event(cursor="test-cursor-2", sequence_number=2),
        verification_id="test-verification-2",
    )
    other_scope = verified_envelope(
        event=inbound_event(
            event_id="test-event-2",
            cursor="test-cursor-2",
            sequence_number=2,
            conversation=conversation(channel_id="test-other-channel"),
            message=message(conversation=conversation(channel_id="test-other-channel")),
            sender=participant(channel_id="test-other-channel"),
            content=content(attachments=(attachment(channel_id="test-other-channel"),)),
        ),
        verification_id="test-verification-2",
    )
    for changes in (
        {"envelopes": (first, duplicate_sequence)},
        {"envelopes": (first, duplicate_event)},
        {"envelopes": (other_scope,)},
        {"envelopes": (first, second), "next_cursor": "test-cursor-1"},
        {"envelopes": (first, second), "next_sequence": 1},
        {"envelopes": (), "has_more": True},
    ):
        with pytest.raises(ValueError):
            inbound_page(**changes)
    for changes in (
        {"next_cursor": None, "next_sequence": 1},
        {"next_cursor": "test-cursor-1", "next_sequence": None},
        {"next_sequence": -1},
        {"next_sequence": True},
        {"has_more": 1},
    ):
        with pytest.raises((TypeError, ValueError)):
            inbound_page(**changes)
    with pytest.raises(TypeError, match="immutable tuple"):
        replace(inbound_page(), envelopes=[first])


def test_inbound_page_validates_request_resume_and_snapshot_bindings() -> None:
    request = inbound_read_request(
        after_cursor="test-cursor-1",
        after_sequence=1,
        snapshot_token="test-snapshot-1",
    )
    next_envelope = verified_envelope(
        event=inbound_event(
            event_id="test-event-2",
            cursor="test-cursor-2",
            sequence_number=2,
        ),
        verification_id="test-verification-2",
    )
    page = inbound_page(request=request, envelopes=(next_envelope,))
    page.validate_request_binding(request)

    invalid_pages = (
        inbound_page(request=request, read_request_id="test-read-request-other"),
        inbound_page(request=request, read_request_digest="d" * 64),
        inbound_page(
            request=request,
            envelopes=(),
            has_more=False,
            channel_id="test-other-channel",
        ),
        inbound_page(request=request, snapshot_token="test-snapshot-other"),
        inbound_page(request=request),
    )
    for invalid in invalid_pages:
        with pytest.raises(ValueError):
            invalid.validate_request_binding(request)

    limited_request = inbound_read_request(limit=1)
    second_envelope = verified_envelope(
        event=inbound_event(
            event_id="test-event-2",
            cursor="test-cursor-2",
            sequence_number=2,
        ),
        verification_id="test-verification-2",
    )
    oversized_page = inbound_page(
        request=limited_request,
        envelopes=(verified_envelope(), second_envelope),
    )
    with pytest.raises(ValueError, match="requested limit"):
        oversized_page.validate_request_binding(limited_request)


def test_empty_inbound_page_preserves_request_pair_and_has_no_more() -> None:
    initial = inbound_read_request()
    inbound_page(
        request=initial,
        envelopes=(),
        next_cursor=None,
        next_sequence=None,
        has_more=False,
    ).validate_request_binding(initial)

    continuation = inbound_read_request(
        after_cursor="test-cursor-4",
        after_sequence=4,
        snapshot_token="test-snapshot-1",
    )
    empty = inbound_page(request=continuation, envelopes=(), has_more=False)
    empty.validate_request_binding(continuation)
    for changes in (
        {"next_cursor": None, "next_sequence": None},
        {"next_cursor": "test-cursor-5", "next_sequence": 5},
    ):
        invalid = inbound_page(
            request=continuation,
            envelopes=(),
            has_more=False,
            **changes,
        )
        with pytest.raises(ValueError, match="preserve"):
            invalid.validate_request_binding(continuation)


def test_inbound_page_binds_exact_capability_revision_digest_and_scope() -> None:
    snapshot = capability()
    page = inbound_page(capability=snapshot)
    page.validate_capability_binding(snapshot)
    for invalid in (
        inbound_page(capability_revision="test-capability-other"),
        inbound_page(capability_digest="d" * 64),
        inbound_page(
            envelopes=(),
            has_more=False,
            channel_id="test-other-channel",
        ),
    ):
        with pytest.raises(ValueError):
            invalid.validate_capability_binding(snapshot)


def test_inbound_page_preflights_nested_and_raw_byte_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = inbound_page().canonical_bytes()
    monkeypatch.setattr(IMInboundPageV1, "_MAX_CANONICAL_BYTES", len(encoded) - 1)
    with pytest.raises(ValueError, match="canonical byte limit"):
        inbound_page()
    with pytest.raises(ValueError, match="byte limit"):
        IMInboundPageV1.from_json_bytes(encoded)


@pytest.mark.parametrize(
    "operation_name",
    [
        "send_message",
        "edit_message",
        "delete_message",
        "add_reaction",
        "remove_reaction",
    ],
)
def test_action_intent_accepts_exact_operation_matrix(operation_name: str) -> None:
    intent = action_intent(operation=operation_name)
    assert intent.operation == operation_name
    assert IMActionIntentV1.from_dict(intent.to_dict()) == intent


def test_action_intent_rejects_every_required_and_forbidden_matrix_drift() -> None:
    invalid_changes = (
        {"target_message": message()},
        {"content": None},
        {"reaction": reaction()},
        {"operation": "edit_message", "target_message": None},
        {"operation": "edit_message", "content": None},
        {"operation": "edit_message", "reaction": reaction()},
        {"operation": "delete_message", "target_message": None},
        {"operation": "delete_message", "content": content()},
        {"operation": "delete_message", "reaction": reaction()},
        {"operation": "add_reaction", "target_message": None},
        {"operation": "add_reaction", "content": content()},
        {"operation": "add_reaction", "reaction": None},
        {"operation": "remove_reaction", "target_message": None},
        {"operation": "remove_reaction", "content": content()},
        {"operation": "remove_reaction", "reaction": None},
        {"operation": "future_operation"},
    )
    for changes in invalid_changes:
        with pytest.raises(ValueError):
            action_intent(**changes)


def test_action_intent_rejects_scope_and_exact_conversation_drift() -> None:
    direct_changes = (
        {"tenant_id": "test-other-tenant"},
        {"workspace_id": "test-other-workspace"},
    )
    target_conversations = (
        conversation(tenant_id="test-other-tenant"),
        conversation(workspace_id="test-other-workspace"),
        conversation(provider="qe.other-im.v1"),
        conversation(channel_id="test-other-channel"),
        conversation(conversation_id="test-other-conversation"),
        conversation(thread_id="test-other-thread"),
    )
    reaction_changes = (
        {"tenant_id": "test-other-tenant"},
        {"workspace_id": "test-other-workspace"},
        {"provider": "qe.other-im.v1"},
        {"channel_id": "test-other-channel"},
    )
    attachment_changes = reaction_changes
    invalid = list(direct_changes)
    invalid.extend(
        {
            "operation": "edit_message",
            "target_message": message(conversation=changed_conversation),
        }
        for changed_conversation in target_conversations
    )
    invalid.extend(
        {"operation": "add_reaction", "reaction": reaction(**changed)}
        for changed in reaction_changes
    )
    invalid.extend(
        {"content": content(attachments=(attachment(**changed),))} for changed in attachment_changes
    )
    for changes in invalid:
        with pytest.raises(ValueError):
            action_intent(**changes)


def test_action_intent_decode_is_exact_and_repr_hides_effect_content() -> None:
    intent = action_intent(delegator_id="test-delegator-1")
    wire = intent.to_dict()
    for changed in (
        {**wire, "future": True},
        {key: item for key, item in wire.items() if key != "actionId"},
        dict(wire, schemaVersion=True),
    ):
        with pytest.raises((TypeError, ValueError)):
            IMActionIntentV1.from_dict(changed)
    with pytest.raises(ValueError, match="duplicate"):
        IMActionIntentV1.from_json_bytes(b'{"schemaVersion":1,"schemaVersion":1}')
    with pytest.raises(TypeError, match="exact"):
        replace(intent, content=object())
    with pytest.raises(TypeError, match="exact"):
        replace(action_intent(operation="edit_message"), target_message=object())
    with pytest.raises(TypeError, match="exact"):
        replace(action_intent(operation="add_reaction"), reaction=object())
    assert "hello" not in repr(intent)
    assert "test-object-1" not in repr(intent)


def test_action_intent_validates_time_trace_and_digest_sensitive_identity() -> None:
    base = action_intent()
    for changes in (
        {"created_at": "2026-08-28T00:00:00Z"},
        {"traceparent": ""},
        {"actor_id": ""},
        {"causation_id": ""},
    ):
        with pytest.raises(ValueError):
            action_intent(**changes)
    assert action_intent(traceparent=None).traceparent is None
    for changed in (
        action_intent(action_id="test-action-2"),
        action_intent(actor_id="test-actor-2"),
        action_intent(delegator_id="test-delegator-1"),
        action_intent(correlation_id="test-correlation-2"),
        action_intent(causation_id="test-causation-2"),
        action_intent(traceparent=None),
        action_intent(conversation=conversation(conversation_id="test-conversation-2")),
        action_intent(content=content(segments=(text_segment("changed"),), attachments=())),
        action_intent(created_at="2026-08-28T00:00:00.000002Z"),
        action_intent(
            operation="edit_message",
            target_message=message(revision="test-message-revision-2"),
        ),
        action_intent(
            operation="add_reaction",
            reaction=reaction(reaction_key="test-heart"),
        ),
    ):
        assert changed.canonical_digest() != base.canonical_digest()


def test_action_intent_preflights_canonical_and_raw_byte_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = action_intent().canonical_bytes()
    monkeypatch.setattr(IMActionIntentV1, "_MAX_CANONICAL_BYTES", len(encoded) - 1)
    with pytest.raises(ValueError, match="canonical byte limit"):
        action_intent()
    with pytest.raises(ValueError, match="byte limit"):
        IMActionIntentV1.from_json_bytes(encoded)


def test_native_im_idempotency_key_matches_frozen_domain_vector() -> None:
    intent = action_intent()
    assert derive_im_idempotency_key_v1(intent) == (
        "ff19f1f4ce9b043052b2ba6ac9f36f1bf105e46743b00e44891c61f9172fcaea"
    )
    assert derive_im_idempotency_key_v1(intent) != intent.canonical_digest()


def test_native_im_idempotency_key_changes_only_with_exact_action_scope() -> None:
    base = action_intent()
    base_key = derive_im_idempotency_key_v1(base)
    same_key_intents = (
        action_intent(actor_id="test-actor-2"),
        action_intent(delegator_id="test-delegator-1"),
        action_intent(content=content(segments=(text_segment("changed"),), attachments=())),
        action_intent(correlation_id="test-correlation-2"),
    )
    assert all(derive_im_idempotency_key_v1(item) == base_key for item in same_key_intents)

    changed_scope_intents = (
        action_intent(action_id="test-action-2"),
        action_intent(
            tenant_id="test-tenant-2",
            conversation=conversation(tenant_id="test-tenant-2"),
            content=content(segments=(text_segment(),), attachments=()),
        ),
        action_intent(
            workspace_id="test-workspace-2",
            conversation=conversation(workspace_id="test-workspace-2"),
            content=content(segments=(text_segment(),), attachments=()),
        ),
        action_intent(
            conversation=conversation(provider="qe.fake-im-v2"),
            content=content(segments=(text_segment(),), attachments=()),
        ),
        action_intent(
            conversation=conversation(channel_id="test-channel-2"),
            content=content(segments=(text_segment(),), attachments=()),
        ),
    )
    assert all(derive_im_idempotency_key_v1(item) != base_key for item in changed_scope_intents)
    with pytest.raises(TypeError, match="exact"):
        derive_im_idempotency_key_v1(object())  # type: ignore[arg-type]


def test_action_command_binds_intent_digest_idempotency_and_trace_context() -> None:
    command = action_command()
    assert command.intent_digest == command.intent.canonical_digest()
    assert command.idempotency_key == derive_im_idempotency_key_v1(command.intent)
    assert command.causation_id == command.intent.action_id
    for changes in (
        {"intent_digest": "d" * 64},
        {"idempotency_key": "d" * 64},
        {"correlation_id": "test-correlation-other"},
        {"causation_id": command.intent.causation_id},
        {"traceparent": None},
    ):
        with pytest.raises(ValueError):
            action_command(**changes)


def test_action_command_requires_exact_approval_pair_and_time_window() -> None:
    approved = action_command(
        approval_decision_id="test-approval-decision-1",
        approval_revision="test-approval-revision-1",
    )
    assert approved.approval_revision == "test-approval-revision-1"
    for changes in (
        {"approval_decision_id": "test-approval-decision-1"},
        {"approval_revision": "test-approval-revision-1"},
        {"authorized_at": LATER_TIME, "expires_at": TIME},
        {"authorized_at": TIME, "expires_at": TIME},
        {"expires_at": "2026-08-28T00:00:01Z"},
    ):
        with pytest.raises(ValueError):
            action_command(**changes)
    historical = action_command(
        authorized_at="2020-01-01T00:00:00.000000Z",
        expires_at="2020-01-01T00:00:01.000000Z",
    )
    assert IMActionCommandV1.from_dict(historical.to_dict()) == historical


def test_action_command_validates_trusted_capability_binding() -> None:
    snapshot = capability()
    command = action_command(capability=snapshot)
    command.validate_capability_binding(snapshot)
    invalid_snapshots = (
        capability(tenant_id="test-other-tenant"),
        capability(workspace_id="test-other-workspace"),
        capability(provider="qe.other-im.v1"),
        capability(channel_id="test-other-channel"),
        capability(revision="test-capability-other"),
        capability(supports_threads=False),
        capability(operations=(operation("edit_message"),)),
    )
    for invalid in invalid_snapshots:
        with pytest.raises(ValueError):
            command.validate_capability_binding(invalid)
    missing_operation = capability(operations=(operation("edit_message"),))
    command_bound_to_missing_operation = action_command(capability=missing_operation)
    with pytest.raises(ValueError, match="does not enable"):
        command_bound_to_missing_operation.validate_capability_binding(missing_operation)
    with pytest.raises(TypeError, match="exact"):
        command.validate_capability_binding(object())  # type: ignore[arg-type]


def test_action_command_decode_is_exact_fresh_and_repr_safe() -> None:
    command = action_command()
    wire = command.to_dict()
    for changed in (
        {**wire, "future": True},
        {key: item for key, item in wire.items() if key != "commandId"},
        dict(wire, schemaVersion=True),
    ):
        with pytest.raises((TypeError, ValueError)):
            IMActionCommandV1.from_dict(changed)
    with pytest.raises(ValueError, match="duplicate"):
        IMActionCommandV1.from_json_bytes(b'{"schemaVersion":1,"schemaVersion":1}')
    with pytest.raises(TypeError, match="exact"):
        replace(command, intent=object())
    wire["intent"]["content"]["segments"][0]["text"] = "mutated"  # type: ignore[index]
    assert command.intent.content is not None
    assert command.intent.content.segments[0].text == "hello\nworld"
    assert "hello" not in repr(command)
    assert "test-object-1" not in repr(command)


def test_action_command_preflights_canonical_and_raw_byte_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = action_command().canonical_bytes()
    monkeypatch.setattr(IMActionCommandV1, "_MAX_CANONICAL_BYTES", len(encoded) - 1)
    with pytest.raises(ValueError, match="canonical byte limit"):
        action_command()
    with pytest.raises(ValueError, match="byte limit"):
        IMActionCommandV1.from_json_bytes(encoded)


def test_dispatch_request_binds_command_attempt_fence_and_trace_context() -> None:
    request = dispatch_request()
    assert request.command_digest == request.command.canonical_digest()
    assert request.causation_id == request.command.command_id
    assert dispatch_request(attempt_number=(1 << 63) - 1).attempt_number == (1 << 63) - 1
    null_trace_command = action_command(intent=action_intent(traceparent=None))
    assert dispatch_request(command=null_trace_command).traceparent is None
    for changes in (
        {"command_digest": "d" * 64},
        {"dispatch_attempt_id": ""},
        {"attempt_number": 0},
        {"attempt_number": -1},
        {"attempt_number": True},
        {"attempt_number": 1.0},
        {"attempt_number": 1 << 63},
        {"fence_id": ""},
        {"fence_revision": ""},
        {"fence_revision": 1},
        {"correlation_id": "test-correlation-other"},
        {"causation_id": request.command.intent.action_id},
        {"traceparent": None},
    ):
        with pytest.raises((TypeError, ValueError)):
            dispatch_request(**changes)


def test_dispatch_request_requires_exact_claim_deadline_window() -> None:
    command = action_command()
    assert dispatch_request(command=command).dispatch_deadline_at == command.expires_at
    assert (
        dispatch_request(
            command=command,
            dispatch_deadline_at="2026-08-28T00:00:00.500001Z",
        ).dispatch_deadline_at
        < command.expires_at
    )
    for changes in (
        {"claimed_at": LATER_TIME, "dispatch_deadline_at": TIME},
        {"claimed_at": TIME, "dispatch_deadline_at": TIME},
        {"dispatch_deadline_at": "2026-08-28T00:00:02.000001Z"},
        {"claimed_at": "2026-08-28T00:00:00Z"},
    ):
        with pytest.raises(ValueError):
            dispatch_request(command=command, **changes)
    historical_command = action_command(
        authorized_at="2020-01-01T00:00:00.000000Z",
        expires_at="2020-01-01T00:00:02.000000Z",
    )
    historical = dispatch_request(
        command=historical_command,
        claimed_at="2020-01-01T00:00:00.000001Z",
        dispatch_deadline_at="2020-01-01T00:00:01.000000Z",
    )
    assert IMDispatchRequestV1.from_dict(historical.to_dict()) == historical


def test_dispatch_request_decode_is_exact_fresh_and_repr_safe() -> None:
    request = dispatch_request()
    wire = request.to_dict()
    for changed in (
        {**wire, "future": True},
        {key: item for key, item in wire.items() if key != "dispatchAttemptId"},
        dict(wire, schemaVersion=True),
    ):
        with pytest.raises((TypeError, ValueError)):
            IMDispatchRequestV1.from_dict(changed)
    with pytest.raises(ValueError, match="duplicate"):
        IMDispatchRequestV1.from_json_bytes(b'{"schemaVersion":1,"schemaVersion":1}')
    with pytest.raises(TypeError, match="exact"):
        replace(request, command=object())
    wire["command"]["intent"]["content"]["segments"][0]["text"] = "mutated"  # type: ignore[index]
    assert request.command.intent.content is not None
    assert request.command.intent.content.segments[0].text == "hello\nworld"
    assert "hello" not in repr(request)
    assert "test-object-1" not in repr(request)


def test_dispatch_request_preflights_canonical_and_raw_byte_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = dispatch_request().canonical_bytes()
    monkeypatch.setattr(IMDispatchRequestV1, "_MAX_CANONICAL_BYTES", len(encoded) - 1)
    with pytest.raises(ValueError, match="canonical byte limit"):
        dispatch_request()
    with pytest.raises(ValueError, match="byte limit"):
        IMDispatchRequestV1.from_json_bytes(encoded)
