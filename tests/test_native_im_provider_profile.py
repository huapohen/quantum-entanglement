from __future__ import annotations

import json
from dataclasses import replace

import pytest

from quantum_entanglement._native_im_codec import NativeIMCodecTooLargeError
from quantum_entanglement.native_im_provider_profile import (
    IMProviderAuthenticationProfileV1,
    IMProviderEventMappingV1,
    IMProviderFeatureV1,
    IMProviderIdentityMappingV1,
    IMProviderLimitProfileV1,
    IMProviderOperationProfileV1,
    IMProviderProfileV1,
    IMProviderResumeProfileV1,
)

SCHEMA = 1
TIME = "2026-08-28T08:00:00.000001Z"
EVIDENCE = "a" * 64

CANONICAL_FIELDS = (
    "attachmentId",
    "attachmentVersion",
    "channelId",
    "conversationId",
    "cursor",
    "eventId",
    "membershipRevision",
    "messageId",
    "messageRevision",
    "participantId",
    "providerMessageId",
    "providerOperationId",
    "reactionKey",
    "sequenceNumber",
    "snapshotToken",
    "tenantId",
    "threadId",
    "workspaceId",
)
EVENT_TYPES = (
    "membership.changed",
    "message.created",
    "message.deleted",
    "message.edited",
    "reaction.added",
    "reaction.removed",
)
FEATURES = ("attachments", "membership_events", "mentions", "threads")
OPERATIONS = (
    "add_reaction",
    "delete_message",
    "edit_message",
    "remove_reaction",
    "send_message",
)


def identity_mapping(
    canonical_field: str = "eventId", **changes: object
) -> IMProviderIdentityMappingV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "canonical_field": canonical_field,
        "status": "supported",
        "provider_json_pointer": f"/event/{canonical_field}",
        "mapping_mode": "opaque_exact",
        "provider_scope": "channel",
        "evidence_digest": EVIDENCE,
    }
    values.update(changes)
    return IMProviderIdentityMappingV1(**values)  # type: ignore[arg-type]


def event_mapping(
    event_type: str = "message.created", **changes: object
) -> IMProviderEventMappingV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "event_type": event_type,
        "status": "supported",
        "provider_event_type": f"provider.{event_type}",
        "evidence_digest": EVIDENCE,
    }
    values.update(changes)
    return IMProviderEventMappingV1(**values)  # type: ignore[arg-type]


def authentication(**changes: object) -> IMProviderAuthenticationProfileV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "status": "supported",
        "verifier_contract_id": "test-verifier-v1",
        "signature_mode": "detached_raw_body",
        "timestamp_mode": "signed_unix_seconds",
        "nonce_mode": "signed_unique",
        "endpoint_binding_mode": "method_host_port_path_body",
        "replay_window_seconds": 300,
        "key_rotation_mode": "kid_routed",
        "evidence_digest": EVIDENCE,
    }
    values.update(changes)
    return IMProviderAuthenticationProfileV1(**values)  # type: ignore[arg-type]


def resume(**changes: object) -> IMProviderResumeProfileV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "status": "supported",
        "dedupe_scope": "tenant_workspace_provider_channel_event",
        "cursor_mode": "provider_durable",
        "sequence_mode": "provider_monotonic",
        "snapshot_mode": "provider_snapshot",
        "event_id_retention_seconds": 86_400,
        "cursor_retention_seconds": 86_400,
        "snapshot_retention_seconds": 3_600,
        "evidence_digest": EVIDENCE,
    }
    values.update(changes)
    return IMProviderResumeProfileV1(**values)  # type: ignore[arg-type]


def feature(name: str = "attachments", **changes: object) -> IMProviderFeatureV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "feature": name,
        "status": "unsupported",
        "evidence_digest": EVIDENCE,
    }
    values.update(changes)
    return IMProviderFeatureV1(**values)  # type: ignore[arg-type]


def limits(**changes: object) -> IMProviderLimitProfileV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "max_raw_event_bytes": 1_024 * 1_024,
        "max_raw_page_bytes": 8 * 1_024 * 1_024,
        "max_page_events": 100,
        "max_text_bytes": 64 * 1_024,
        "max_attachments": 8,
        "max_attachment_bytes": 16 * 1_024 * 1_024,
        "requests_per_window": 100,
        "rate_limit_window_seconds": 60,
        "retry_after_mode": "delta_seconds",
        "evidence_digest": EVIDENCE,
    }
    values.update(changes)
    return IMProviderLimitProfileV1(**values)  # type: ignore[arg-type]


def operation(name: str = "send_message", **changes: object) -> IMProviderOperationProfileV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "operation": name,
        "status": "unsupported",
        "capability": None,
        "evidence_digest": EVIDENCE,
    }
    values.update(changes)
    return IMProviderOperationProfileV1(**values)  # type: ignore[arg-type]


def profile(**changes: object) -> IMProviderProfileV1:
    values: dict[str, object] = {
        "schema_version": SCHEMA,
        "profile_id": "test-native-im-profile",
        "revision": "test-revision-1",
        "observed_at": TIME,
        "tenant_id": "test-tenant",
        "workspace_id": "test-workspace",
        "provider": "test-provider",
        "channel_id": "test-channel",
        "environment_class": "sandbox",
        "tenant_mapping_revision": "test-mapping-1",
        "service_account_participant_id": "test-service-account",
        "allowed_conversation_ids": ("test-conversation-a", "test-conversation-b"),
        "event_schema_id": "test-event-schema",
        "event_schema_version": "test-event-schema-v1",
        "identity_mappings": tuple(identity_mapping(name) for name in CANONICAL_FIELDS),
        "event_mappings": tuple(event_mapping(name) for name in EVENT_TYPES),
        "authentication": authentication(),
        "resume": resume(),
        "features": tuple(feature(name) for name in FEATURES),
        "limits": limits(),
        "operations": tuple(operation(name) for name in OPERATIONS),
        "source_evidence_digest": "b" * 64,
    }
    values.update(changes)
    return IMProviderProfileV1(**values)  # type: ignore[arg-type]


def test_profile_round_trip_and_domain_separated_digest_are_stable() -> None:
    value = profile()
    wire = value.to_dict()
    encoded = value.canonical_bytes()

    assert IMProviderProfileV1.from_dict(wire) == value
    assert IMProviderProfileV1.from_json_bytes(encoded) == value
    assert (
        encoded
        == json.dumps(
            wire,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    assert len(value.canonical_digest()) == 64
    assert replace(value, revision="test-revision-2").canonical_digest() != value.canonical_digest()


@pytest.mark.parametrize(
    ("status", "details", "evidence", "accepted"),
    (
        ("supported", ("/event/id", "opaque_exact", "channel"), EVIDENCE, True),
        ("supported", (None, None, None), EVIDENCE, False),
        ("unsupported", (None, None, None), EVIDENCE, True),
        ("unsupported", ("/event/id", "opaque_exact", "channel"), EVIDENCE, False),
        ("unverified", (None, None, None), None, True),
        ("unverified", (None, None, None), EVIDENCE, False),
        ("unknown", (None, None, None), None, False),
    ),
)
def test_identity_mapping_has_one_exact_three_state_matrix(
    status: str,
    details: tuple[str | None, str | None, str | None],
    evidence: str | None,
    accepted: bool,
) -> None:
    kwargs = {
        "status": status,
        "provider_json_pointer": details[0],
        "mapping_mode": details[1],
        "provider_scope": details[2],
        "evidence_digest": evidence,
    }
    if accepted:
        identity_mapping(**kwargs)
    else:
        with pytest.raises((TypeError, ValueError)):
            identity_mapping(**kwargs)


@pytest.mark.parametrize("pointer", ("event/id", "/event/~2id", "/event/~", ""))
def test_provider_json_pointer_rejects_ambiguous_forms(pointer: str) -> None:
    with pytest.raises(ValueError):
        identity_mapping(provider_json_pointer=pointer)


@pytest.mark.parametrize(
    ("factory", "unsupported_changes", "unverified_changes"),
    (
        (
            event_mapping,
            {"status": "unsupported", "provider_event_type": None},
            {"status": "unverified", "provider_event_type": None, "evidence_digest": None},
        ),
        (
            authentication,
            {
                "status": "unsupported",
                "verifier_contract_id": None,
                "signature_mode": None,
                "timestamp_mode": None,
                "nonce_mode": None,
                "endpoint_binding_mode": None,
                "replay_window_seconds": None,
                "key_rotation_mode": None,
            },
            {
                "status": "unverified",
                "verifier_contract_id": None,
                "signature_mode": None,
                "timestamp_mode": None,
                "nonce_mode": None,
                "endpoint_binding_mode": None,
                "replay_window_seconds": None,
                "key_rotation_mode": None,
                "evidence_digest": None,
            },
        ),
        (
            resume,
            {
                "status": "unsupported",
                "dedupe_scope": None,
                "cursor_mode": None,
                "sequence_mode": None,
                "snapshot_mode": None,
                "event_id_retention_seconds": None,
                "cursor_retention_seconds": None,
                "snapshot_retention_seconds": None,
            },
            {
                "status": "unverified",
                "dedupe_scope": None,
                "cursor_mode": None,
                "sequence_mode": None,
                "snapshot_mode": None,
                "event_id_retention_seconds": None,
                "cursor_retention_seconds": None,
                "snapshot_retention_seconds": None,
                "evidence_digest": None,
            },
        ),
        (
            feature,
            {"status": "unsupported"},
            {"status": "unverified", "evidence_digest": None},
        ),
        (
            operation,
            {"status": "unsupported", "capability": None},
            {
                "status": "unverified",
                "capability": None,
                "evidence_digest": None,
            },
        ),
    ),
)
def test_every_profile_component_preserves_unsupported_and_unverified(
    factory: object,
    unsupported_changes: dict[str, object],
    unverified_changes: dict[str, object],
) -> None:
    callable_factory = factory  # keep parametrized failure output readable
    callable_factory(**unsupported_changes)  # type: ignore[operator]
    callable_factory(**unverified_changes)  # type: ignore[operator]

    with pytest.raises(ValueError):
        callable_factory(**{**unverified_changes, "evidence_digest": EVIDENCE})  # type: ignore[operator]


def test_authentication_and_resume_require_all_supported_facts() -> None:
    for field_name in (
        "verifier_contract_id",
        "signature_mode",
        "timestamp_mode",
        "nonce_mode",
        "endpoint_binding_mode",
        "replay_window_seconds",
        "key_rotation_mode",
    ):
        with pytest.raises(ValueError):
            authentication(**{field_name: None})
    for field_name in (
        "dedupe_scope",
        "cursor_mode",
        "sequence_mode",
        "snapshot_mode",
        "event_id_retention_seconds",
        "cursor_retention_seconds",
        "snapshot_retention_seconds",
    ):
        with pytest.raises(ValueError):
            resume(**{field_name: None})


def test_limits_are_exact_bounded_and_rate_limit_fields_are_atomic() -> None:
    limits(
        max_raw_event_bytes=3 * 1_024 * 1_024,
        max_raw_page_bytes=16 * 1_024 * 1_024,
        max_page_events=1_000,
        max_text_bytes=1 * 1_024 * 1_024,
        max_attachments=64,
    )
    limits(
        requests_per_window=None,
        rate_limit_window_seconds=None,
        retry_after_mode="unavailable",
    )
    for changes in (
        {"max_raw_event_bytes": 0},
        {"max_raw_event_bytes": 3 * 1_024 * 1_024 + 1},
        {"max_raw_page_bytes": 16 * 1_024 * 1_024 + 1},
        {"max_page_events": 1_001},
        {"max_text_bytes": 1 * 1_024 * 1_024 + 1},
        {"max_attachments": 65},
        {"max_attachments": True},
        {"requests_per_window": None},
        {"rate_limit_window_seconds": None},
        {"retry_after_mode": "unavailable"},
    ):
        with pytest.raises((TypeError, ValueError)):
            limits(**changes)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("identity_mappings", ()),
        ("event_mappings", ()),
        ("features", ()),
        ("operations", ()),
    ),
)
def test_profile_requires_every_complete_canonical_table(
    field_name: str, replacement: tuple[object, ...]
) -> None:
    with pytest.raises(ValueError):
        profile(**{field_name: replacement})


def test_profile_rejects_reordered_duplicate_and_mutable_tables() -> None:
    value = profile()
    for changes in (
        {"identity_mappings": tuple(reversed(value.identity_mappings))},
        {
            "event_mappings": (
                value.event_mappings[0],
                value.event_mappings[0],
                *value.event_mappings[2:],
            )
        },
        {"features": list(value.features)},
        {"operations": tuple(reversed(value.operations))},
        {"allowed_conversation_ids": tuple(reversed(value.allowed_conversation_ids))},
    ):
        with pytest.raises((TypeError, ValueError)):
            profile(**changes)


def test_exact_decoders_reject_missing_extra_duplicate_and_future_schema() -> None:
    value = profile()
    wire = value.to_dict()
    missing = dict(wire)
    missing.pop("revision")
    extra = dict(wire, futureField=True)
    future = dict(wire, schemaVersion=2)

    for changed in (missing, extra, future):
        with pytest.raises((TypeError, ValueError)):
            IMProviderProfileV1.from_dict(changed)
    with pytest.raises(ValueError):
        IMProviderProfileV1.from_json_bytes(
            value.canonical_bytes()[:-1] + b',"revision":"duplicate"}'
        )
    with pytest.raises(TypeError):
        IMProviderProfileV1.from_json_bytes(bytearray(value.canonical_bytes()))


def test_exact_models_reject_subclasses_and_instance_shadowing() -> None:
    class ProfileSubclass(IMProviderProfileV1):
        pass

    value = profile()
    with pytest.raises(TypeError):
        ProfileSubclass(**value.__dict__)
    with pytest.raises((AttributeError, TypeError)):
        value.revision = "changed"  # type: ignore[misc]


def test_profile_top_level_byte_limit_is_enforced() -> None:
    conversations = tuple(f"{index:03d}-" + "x" * 4_000 for index in range(128))
    with pytest.raises(NativeIMCodecTooLargeError):
        profile(allowed_conversation_ids=conversations)


def test_repr_omits_provider_paths_evidence_tables_and_source_digest() -> None:
    mapping = identity_mapping("attachmentId", provider_json_pointer="/private/canary")
    assert "/private/canary" not in repr(mapping)
    assert EVIDENCE not in repr(mapping)

    value = profile(identity_mappings=(mapping,) + profile().identity_mappings[1:])
    rendered = repr(value)
    assert "/private/canary" not in rendered
    assert EVIDENCE not in rendered
    assert "b" * 64 not in rendered
