from __future__ import annotations

from dataclasses import replace

import pytest

from quantum_entanglement.native_im import InboundIMEventV1
from tests.test_native_im_contract import (
    SCHEMA,
    IMMembershipChangeV1,
    acceptance_query,
    action_receipt,
    attachment,
    capability,
    content,
    conversation,
    inbound_event,
    inbound_page,
    inbound_read_request,
    lookup,
    mention_segment,
    message,
    operation,
    participant,
    reaction,
    text_segment,
    verified_envelope,
)

EVENT_MATRIX = {
    "message.created": {
        "message": "required",
        "sender": "required",
        "content": "required",
        "reaction": "null",
        "membership_change": "null",
    },
    "message.edited": {
        "message": "required",
        "sender": "required",
        "content": "required",
        "reaction": "null",
        "membership_change": "null",
    },
    "message.deleted": {
        "message": "required",
        "sender": "optional",
        "content": "null",
        "reaction": "null",
        "membership_change": "null",
    },
    "reaction.added": {
        "message": "required",
        "sender": "required",
        "content": "null",
        "reaction": "required",
        "membership_change": "null",
    },
    "reaction.removed": {
        "message": "required",
        "sender": "required",
        "content": "null",
        "reaction": "required",
        "membership_change": "null",
    },
    "membership.changed": {
        "message": "null",
        "sender": "optional",
        "content": "null",
        "reaction": "null",
        "membership_change": "required",
    },
}

FIELD_VALUES = {
    "message": message(),
    "sender": participant(),
    "content": content(),
    "reaction": reaction(),
    "membership_change": IMMembershipChangeV1(SCHEMA, participant(), "left", "test-membership-1"),
}


def event_for(event_type: str, **changes: object) -> InboundIMEventV1:
    fields = EVENT_MATRIX[event_type]
    values = {
        field: (None if requirement == "null" else FIELD_VALUES[field])
        for field, requirement in fields.items()
    }
    values.update(changes)
    return inbound_event(event_type=event_type, **values)


@pytest.mark.parametrize(
    ("event_type", "field", "requirement"),
    [
        (event_type, field, requirement)
        for event_type, fields in EVENT_MATRIX.items()
        for field, requirement in fields.items()
    ],
)
def test_every_inbound_event_matrix_cell_is_enforced(
    event_type: str, field: str, requirement: str
) -> None:
    value = event_for(event_type)
    assert InboundIMEventV1.from_dict(value.to_dict()) == value
    if requirement == "required":
        with pytest.raises(ValueError, match="eventType matrix"):
            event_for(event_type, **{field: None})
    elif requirement == "null":
        with pytest.raises(ValueError, match="eventType matrix"):
            event_for(event_type, **{field: FIELD_VALUES[field]})
    else:
        assert event_for(event_type, **{field: None}).event_type == event_type
        assert event_for(event_type, **{field: FIELD_VALUES[field]}).event_type == event_type


@pytest.mark.parametrize(
    ("axis", "changed"),
    [
        ("tenant_id", "test-other-tenant"),
        ("workspace_id", "test-other-workspace"),
        ("provider", "qe.other-im.v1"),
        ("channel_id", "test-other-channel"),
    ],
)
def test_every_inbound_nested_scope_axis_fails_closed(axis: str, changed: str) -> None:
    with pytest.raises(ValueError, match="sender scope"):
        event_for("message.created", sender=participant(**{axis: changed}))
    with pytest.raises(ValueError, match="attachment scope"):
        event_for(
            "message.created",
            content=content(attachments=(attachment(**{axis: changed}),)),
        )
    with pytest.raises(ValueError, match="reaction scope"):
        event_for("reaction.added", reaction=reaction(**{axis: changed}))
    with pytest.raises(ValueError, match="membership change scope"):
        event_for(
            "membership.changed",
            membership_change=IMMembershipChangeV1(
                SCHEMA,
                participant(**{axis: changed}),
                "left",
                "test-membership-1",
            ),
        )


@pytest.mark.parametrize(
    "conversation_changes",
    [
        {"tenant_id": "test-other-tenant"},
        {"workspace_id": "test-other-workspace"},
        {"provider": "qe.other-im.v1"},
        {"channel_id": "test-other-channel"},
        {"conversation_id": "test-other-conversation"},
        {"thread_id": "test-other-thread"},
    ],
)
def test_inbound_message_requires_exact_conversation(conversation_changes: dict[str, str]) -> None:
    changed_message = message(conversation=conversation(**conversation_changes))
    with pytest.raises(ValueError, match="message conversation"):
        event_for("message.created", message=changed_message)


@pytest.mark.parametrize(
    ("operation_name", "revision_mode", "allowed"),
    [
        (name, mode, (name in {"edit_message", "delete_message"}) == (mode != "not_applicable"))
        for name in (
            "send_message",
            "edit_message",
            "delete_message",
            "add_reaction",
            "remove_reaction",
        )
        for mode in ("not_applicable", "required_cas", "provider_best_effort")
    ],
)
def test_complete_operation_revision_matrix(
    operation_name: str, revision_mode: str, allowed: bool
) -> None:
    if allowed:
        assert operation(operation_name, revision_mode=revision_mode).revision_mode == revision_mode
    else:
        with pytest.raises(ValueError, match="revision mode"):
            operation(operation_name, revision_mode=revision_mode)


def test_duplicate_mentions_and_positions_survive_round_trip() -> None:
    duplicate = mention_segment("test-human")
    first = content(
        segments=(duplicate, duplicate, text_segment("tail")),
        attachments=(),
    )
    moved = content(
        segments=(duplicate, text_segment("tail"), duplicate),
        attachments=(),
    )
    decoded = type(first).from_json_bytes(first.canonical_bytes())
    assert decoded.segments == first.segments
    assert [segment.participant_id for segment in decoded.segments].count("test-human") == 2
    assert first.canonical_bytes() != moved.canonical_bytes()
    assert first.canonical_digest() != moved.canonical_digest()


@pytest.mark.parametrize(
    ("axis", "changed"),
    [
        ("tenant_id", "test-other-tenant"),
        ("workspace_id", "test-other-workspace"),
        ("provider", "qe.other-im.v1"),
        ("channel_id", "test-other-channel"),
    ],
)
def test_inbound_page_bindings_cover_every_scope_axis(axis: str, changed: str) -> None:
    request = inbound_read_request()
    snapshot = capability()
    page = inbound_page(request=request, capability=snapshot)
    with pytest.raises(ValueError, match="scope"):
        page.validate_request_binding(replace(request, **{axis: changed}))
    with pytest.raises(ValueError, match="scope"):
        page.validate_capability_binding(replace(snapshot, **{axis: changed}))


@pytest.mark.parametrize(
    ("base", "mutant"),
    [
        (conversation(), conversation(thread_id="test-thread-1")),
        (participant(), participant(membership_revision="test-membership-3")),
        (attachment(), attachment(version="test-attachment-revision-2")),
        (attachment(), attachment(sha256="d" * 64)),
        (inbound_event(), inbound_event(occurred_at="2026-08-28T00:00:01.000001Z")),
        (
            inbound_event(),
            inbound_event(first_received_at="2026-08-28T00:00:01.000001Z"),
        ),
        (inbound_event(), inbound_event(ingress_request_id="test-ingress-request-2")),
        (inbound_event(), inbound_event(causation_id="test-upstream-event-1")),
        (inbound_event(), inbound_event(transport_evidence_digest="d" * 64)),
        (capability(), capability(supports_threads=False)),
        (capability(), capability(supports_mentions=False)),
        (capability(), capability(supports_attachments=False)),
        (capability(), capability(supports_membership_events=False)),
        (capability(), capability(max_text_bytes=2_048)),
        (capability(), capability(max_attachments=5)),
        (capability(), capability(max_attachment_bytes=2_097_152)),
        (
            capability(),
            capability(
                operations=(
                    operation(
                        acceptance_lookups=(
                            lookup(consistency_seconds=6),
                            lookup("provider_operation_id"),
                        )
                    ),
                )
            ),
        ),
        (verified_envelope(), verified_envelope(verification_id="test-verification-2")),
        (
            action_receipt("succeeded"),
            action_receipt("succeeded", receipt_id="test-receipt-2"),
        ),
        (
            action_receipt("succeeded"),
            action_receipt("succeeded", observed_at="2026-08-28T00:00:03.000001Z"),
        ),
        (acceptance_query(), acceptance_query(query_id="test-query-2")),
        (
            acceptance_query(),
            acceptance_query(requested_at="2026-08-28T00:00:04.000001Z"),
        ),
    ],
)
def test_high_risk_field_mutations_change_domain_digest(base: object, mutant: object) -> None:
    assert type(base) is type(mutant)
    assert base.canonical_digest() != mutant.canonical_digest()  # type: ignore[attr-defined]
