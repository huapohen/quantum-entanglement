from __future__ import annotations

import json
from dataclasses import replace

import pytest

from quantum_entanglement.native_im_inbox import (
    NativeIMInboundCheckpointConflictError,
    NativeIMInboundCheckpointV1,
    NativeIMInboundCommitAmbiguityError,
    NativeIMInboundConflictError,
    NativeIMInboundPageAdmissionResultV1,
    NativeIMInboundReadPreparationV1,
    NativeIMInboundTransactionError,
    NativeIMInboxEventReceiptV1,
    NativeIMScopeV1,
)

SCHEMA = 1
TIME = "2026-08-28T12:00:00.000001Z"


def scope(**changes: object) -> NativeIMScopeV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": "test-provider",
        "channel_id": "test-channel",
    }
    values.update(changes)
    return NativeIMScopeV1(**values)  # type: ignore[arg-type]


def event_receipt(number: int = 1, **changes: object) -> NativeIMInboxEventReceiptV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "scope": scope(),
        "event_id": f"test-event-{number}",
        "event_digest": f"{number:x}" * 64,
        "cursor": f"test-cursor-{number}",
        "sequence_number": number,
        "first_received_at": TIME,
        "admitted_at": TIME,
    }
    values.update(changes)
    return NativeIMInboxEventReceiptV1(**values)  # type: ignore[arg-type]


def checkpoint(**changes: object) -> NativeIMInboundCheckpointV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "scope": scope(),
        "after_cursor": "test-cursor-2",
        "after_sequence": 2,
        "continuation_snapshot_token": "test-snapshot-1",
        "checkpoint_revision": 1,
        "last_read_request_digest": "a" * 64,
        "last_page_digest": "b" * 64,
        "updated_at": TIME,
    }
    values.update(changes)
    return NativeIMInboundCheckpointV1(**values)  # type: ignore[arg-type]


def preparation(**changes: object) -> NativeIMInboundReadPreparationV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "scope": scope(),
        "read_request_id": "test-read-1",
        "read_request_digest": "a" * 64,
        "base_checkpoint_revision": 0,
        "read_status": "prepared",
        "disposition": "fresh_observation",
        "prepared_at": TIME,
    }
    values.update(changes)
    return NativeIMInboundReadPreparationV1(**values)  # type: ignore[arg-type]


def admission(**changes: object) -> NativeIMInboundPageAdmissionResultV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "scope": scope(),
        "read_request_id": "test-read-1",
        "read_request_digest": "a" * 64,
        "page_digest": "b" * 64,
        "disposition": "fresh_observation",
        "checkpoint": checkpoint(),
        "event_receipts": (event_receipt(1), event_receipt(2)),
        "admitted_at": TIME,
    }
    values.update(changes)
    return NativeIMInboundPageAdmissionResultV1(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("factory", "model"),
    (
        (scope, NativeIMScopeV1),
        (event_receipt, NativeIMInboxEventReceiptV1),
        (checkpoint, NativeIMInboundCheckpointV1),
        (preparation, NativeIMInboundReadPreparationV1),
        (admission, NativeIMInboundPageAdmissionResultV1),
    ),
)
def test_every_inbox_receipt_has_strict_canonical_round_trip(factory: object, model: type) -> None:
    value = factory()  # type: ignore[operator]
    encoded = value.canonical_bytes()
    assert model.from_dict(value.to_dict()) == value
    assert model.from_json_bytes(encoded) == value
    assert (
        encoded
        == json.dumps(
            value.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    assert len(value.canonical_digest()) == 64


def test_scope_and_event_receipt_bind_all_identity_axes() -> None:
    baseline = event_receipt()
    for field_name in ("tenant_id", "workspace_id", "provider", "channel_id"):
        changed_scope = replace(scope(), **{field_name: f"other-{field_name}"})
        changed = replace(baseline, scope=changed_scope)
        assert changed.canonical_digest() != baseline.canonical_digest()
    for field_name, value in (
        ("event_id", "other-event"),
        ("event_digest", "f" * 64),
        ("cursor", "other-cursor"),
        ("sequence_number", 2),
        ("first_received_at", "2026-08-28T12:00:01.000001Z"),
        ("admitted_at", "2026-08-28T12:00:01.000001Z"),
    ):
        assert (
            replace(baseline, **{field_name: value}).canonical_digest()
            != baseline.canonical_digest()
        )


def test_checkpoint_allows_initial_empty_terminal_state_but_requires_cursor_pairs() -> None:
    checkpoint(
        after_cursor=None,
        after_sequence=None,
        continuation_snapshot_token=None,
    )
    for changes in (
        {"after_cursor": None},
        {"after_sequence": None},
        {
            "after_cursor": None,
            "after_sequence": None,
            "continuation_snapshot_token": "snapshot-without-cursor",
        },
        {"checkpoint_revision": 0},
        {"checkpoint_revision": True},
    ):
        with pytest.raises((TypeError, ValueError)):
            checkpoint(**changes)


@pytest.mark.parametrize(
    ("read_status", "disposition", "accepted"),
    (
        ("prepared", "fresh_observation", True),
        ("prepared", "observed_replay", True),
        ("admitted", "observed_replay", True),
        ("admitted", "fresh_observation", False),
        ("future", "observed_replay", False),
        ("prepared", "accepted", False),
    ),
)
def test_preparation_uses_observation_language_and_exact_status_matrix(
    read_status: str, disposition: str, accepted: bool
) -> None:
    if accepted:
        preparation(read_status=read_status, disposition=disposition)
    else:
        with pytest.raises(ValueError):
            preparation(read_status=read_status, disposition=disposition)


def test_page_admission_binds_checkpoint_page_request_scope_and_ordered_receipts() -> None:
    value = admission()
    assert value.checkpoint.last_read_request_digest == value.read_request_digest
    assert value.checkpoint.last_page_digest == value.page_digest
    assert tuple(item.sequence_number for item in value.event_receipts) == (1, 2)

    for changes in (
        {"checkpoint": replace(checkpoint(), scope=scope(channel_id="other-channel"))},
        {"checkpoint": replace(checkpoint(), last_read_request_digest="f" * 64)},
        {"checkpoint": replace(checkpoint(), last_page_digest="f" * 64)},
        {"event_receipts": (event_receipt(2), event_receipt(1))},
        {"event_receipts": (event_receipt(1), event_receipt(1))},
        {
            "event_receipts": (
                event_receipt(1),
                event_receipt(2, scope=scope(channel_id="other-channel")),
            )
        },
        {"event_receipts": [event_receipt(1)]},
    ):
        with pytest.raises((TypeError, ValueError)):
            admission(**changes)


def test_page_admission_repr_does_not_expand_event_receipts() -> None:
    value = admission()
    rendered = repr(value)
    assert "test-event-1" not in rendered
    assert "test-cursor-1" not in rendered


def test_exact_decoders_reject_unknown_missing_duplicate_and_future_fields() -> None:
    value = admission()
    wire = value.to_dict()
    missing = dict(wire)
    missing.pop("pageDigest")
    for changed in (missing, dict(wire, future=True), dict(wire, schemaVersion=2)):
        with pytest.raises((TypeError, ValueError)):
            NativeIMInboundPageAdmissionResultV1.from_dict(changed)
    with pytest.raises(ValueError):
        NativeIMInboundPageAdmissionResultV1.from_json_bytes(
            value.canonical_bytes()[:-1] + b',"pageDigest":"duplicate"}'
        )
    with pytest.raises(TypeError):
        NativeIMInboundPageAdmissionResultV1.from_json_bytes(bytearray(value.canonical_bytes()))


def test_models_reject_subclasses_and_mutation() -> None:
    class ScopeSubclass(NativeIMScopeV1):
        pass

    with pytest.raises(TypeError):
        ScopeSubclass(
            schema_version=1,
            tenant_id="test-tenant",
            workspace_id="test-workspace",
            provider="test-provider",
            channel_id="test-channel",
        )
    value = checkpoint()
    with pytest.raises((AttributeError, TypeError)):
        value.checkpoint_revision = 2  # type: ignore[misc]


def test_error_hierarchy_keeps_conflict_transaction_and_ambiguity_distinct() -> None:
    assert issubclass(NativeIMInboundCheckpointConflictError, NativeIMInboundConflictError)
    assert issubclass(NativeIMInboundCommitAmbiguityError, NativeIMInboundTransactionError)
    assert not issubclass(NativeIMInboundConflictError, NativeIMInboundTransactionError)
