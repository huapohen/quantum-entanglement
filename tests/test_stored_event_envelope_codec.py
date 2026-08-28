from __future__ import annotations

import hashlib
from typing import Any

import pytest

import quantum_entanglement
from quantum_entanglement import _stored_event_envelope_codec as codec

BASE_VALUES: dict[str, object] = {
    "event_id": "event-result-1",
    "stream_id": "session:alpha",
    "event_type": "task.invocation.result.accepted",
    "actor_id": "agent:worker-1",
    "timestamp": "2026-08-28T09:10:11.123456Z",
    "correlation_id": "corr-1",
    "causation_id": "event-start-1",
    "idempotency_key": "result:event-1",
    "payload_json": '{"artifactCount":2,"narration":"完成","nested":{"ok":true}}',
    "sequence": 7,
    "global_position": 19,
}
EXPECTED_CANONICAL_BYTES = (
    b'{"actorId":"agent:worker-1","causationId":"event-start-1",'
    b'"correlationId":"corr-1","eventId":"event-result-1",'
    b'"eventType":"task.invocation.result.accepted","globalPosition":19,'
    b'"idempotencyKey":"result:event-1","payload":{"artifactCount":2,'
    b'"narration":"\xe5\xae\x8c\xe6\x88\x90","nested":{"ok":true}},'
    b'"schemaVersion":1,"sequence":7,"streamId":"session:alpha",'
    b'"timestamp":"2026-08-28T09:10:11.123456Z"}'
)
EXPECTED_DIGEST = "3d395de9f8a0ba6ac163693f17fddee035a76670392e776c0711e7e7a61491ba"


def envelope(**overrides: object) -> codec._StoredEventEnvelopeV1:
    values = {**BASE_VALUES, **overrides}
    return codec._stored_event_envelope_from_values(**values)


def test_canonical_body_and_domain_separated_digest_are_exact() -> None:
    value = envelope()

    assert value.to_dict() == {
        "schemaVersion": 1,
        "eventId": "event-result-1",
        "streamId": "session:alpha",
        "eventType": "task.invocation.result.accepted",
        "actorId": "agent:worker-1",
        "timestamp": "2026-08-28T09:10:11.123456Z",
        "correlationId": "corr-1",
        "causationId": "event-start-1",
        "idempotencyKey": "result:event-1",
        "payload": {
            "artifactCount": 2,
            "narration": "完成",
            "nested": {"ok": True},
        },
        "sequence": 7,
        "globalPosition": 19,
    }
    assert value.canonical_bytes() == EXPECTED_CANONICAL_BYTES
    assert value.digest() == EXPECTED_DIGEST
    assert value.digest() == hashlib.sha256(
        codec.STORED_EVENT_ENVELOPE_DOMAIN.encode("utf-8") + EXPECTED_CANONICAL_BYTES
    ).hexdigest()


def test_private_codec_is_not_exported_and_value_is_capability_free() -> None:
    value = envelope()

    assert "_StoredEventEnvelopeV1" not in quantum_entanglement.__all__
    assert "_stored_event_envelope_from_values" not in quantum_entanglement.__all__
    assert repr(value) == "_StoredEventEnvelopeV1(<capability-free>)"
    assert not hasattr(value, "__dict__")
    assert not hasattr(value, "accepted")
    assert not hasattr(value, "commit")

    with pytest.raises(AttributeError, match="immutable"):
        value.event_id = "forged"  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        object.__setattr__(value, "canonical_bytes", lambda: b"forged")


def test_returned_payload_is_a_detached_copy_and_internal_tampering_fails_closed() -> None:
    value = envelope()
    first = value.to_dict()
    payload = first["payload"]
    assert type(payload) is dict
    payload["artifactCount"] = 999  # type: ignore[index]

    assert value.to_dict()["payload"] == {
        "artifactCount": 2,
        "narration": "完成",
        "nested": {"ok": True},
    }
    assert value.digest() == EXPECTED_DIGEST

    object.__setattr__(
        value,
        "_StoredEventEnvelopeV1__payload_json",
        '{"artifactCount": 999}',
    )
    with pytest.raises(codec.StoredEventEnvelopeCanonicalError):
        value.canonical_bytes()


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("event_id", "event-result-2"),
        ("stream_id", "session:beta"),
        ("event_type", "task.status.changed"),
        ("actor_id", "agent:worker-2"),
        ("timestamp", "2026-08-28T09:10:12.123456Z"),
        ("correlation_id", None),
        ("causation_id", None),
        ("idempotency_key", None),
        (
            "payload_json",
            '{"artifactCount":3,"narration":"完成","nested":{"ok":true}}',
        ),
        ("sequence", 8),
        ("global_position", 20),
    ),
)
def test_every_stored_row_field_changes_the_digest(field: str, replacement: object) -> None:
    assert envelope(**{field: replacement}).digest() != EXPECTED_DIGEST


@pytest.mark.parametrize(
    ("field", "invalid", "error"),
    (
        ("event_id", b"event-result-1", codec.StoredEventEnvelopeTypeError),
        ("stream_id", " session:alpha", codec.StoredEventEnvelopeCanonicalError),
        ("event_type", "", codec.StoredEventEnvelopeError),
        ("actor_id", "agent:\u0000worker", codec.StoredEventEnvelopeCanonicalError),
        ("correlation_id", 1, codec.StoredEventEnvelopeTypeError),
        ("causation_id", "e\u0301", codec.StoredEventEnvelopeCanonicalError),
        ("payload_json", b"{}", codec.StoredEventEnvelopeTypeError),
        ("sequence", True, codec.StoredEventEnvelopeTypeError),
        ("sequence", 0, codec.StoredEventEnvelopeError),
        ("sequence", 1 << 63, codec.StoredEventEnvelopeError),
        ("global_position", False, codec.StoredEventEnvelopeTypeError),
        ("global_position", -1, codec.StoredEventEnvelopeError),
    ),
)
def test_exact_scalar_types_and_bounds_are_enforced(
    field: str,
    invalid: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        envelope(**{field: invalid})


@pytest.mark.parametrize(
    "invalid",
    (
        "2026-08-28T09:10:11Z",
        "2026-08-28T09:10:11.123456+00:00",
        "2026-08-28T09:10:11.123456z",
        "2026-02-29T09:10:11.123456Z",
        "2026-08-28T24:10:11.123456Z",
    ),
)
def test_timestamp_requires_real_canonical_utc_microseconds(invalid: str) -> None:
    with pytest.raises(codec.StoredEventEnvelopeCanonicalError):
        envelope(timestamp=invalid)


def test_exact_class_and_class_qualified_methods_reject_hostile_shapes() -> None:
    class StringSubclass(str):
        pass

    class EnvelopeSubclass(codec._StoredEventEnvelopeV1):
        pass

    with pytest.raises(codec.StoredEventEnvelopeTypeError):
        envelope(event_id=StringSubclass("event-result-1"))
    with pytest.raises(codec.StoredEventEnvelopeTypeError):
        EnvelopeSubclass(**BASE_VALUES)

    hostile = object.__new__(EnvelopeSubclass)
    with pytest.raises(codec.StoredEventEnvelopeTypeError):
        codec._StoredEventEnvelopeV1.canonical_bytes(hostile)

    malformed = object.__new__(codec._StoredEventEnvelopeV1)
    with pytest.raises(codec.StoredEventEnvelopeCanonicalError):
        codec._StoredEventEnvelopeV1.digest(malformed)


def test_optional_row_identities_are_canonical_nullable_text() -> None:
    value = envelope(correlation_id=None, causation_id=None, idempotency_key=None)

    assert value.to_dict()["correlationId"] is None
    assert value.to_dict()["causationId"] is None
    assert value.to_dict()["idempotencyKey"] is None


def test_factory_rejects_unknown_or_missing_fields_at_python_boundary() -> None:
    with pytest.raises(TypeError):
        codec._stored_event_envelope_from_values(**{**BASE_VALUES, "future": True})

    missing: dict[str, Any] = dict(BASE_VALUES)
    del missing["event_id"]
    with pytest.raises(TypeError):
        codec._stored_event_envelope_from_values(**missing)
