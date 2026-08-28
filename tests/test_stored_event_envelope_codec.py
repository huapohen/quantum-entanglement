from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

import pytest

import quantum_entanglement
from quantum_entanglement import _stored_event_envelope_codec as codec

BASE_VALUES: dict[str, object] = {
    "event_id": "event-golden-1",
    "stream_id": "session:alpha",
    "event_type": "codec.golden.checked",
    "actor_id": "agent:worker-1",
    "timestamp": "2026-08-28T09:10:11.123456Z",
    "correlation_id": "corr-1",
    "causation_id": "event-start-1",
    "idempotency_key": "codec:event-1",
    "payload_json": '{"artifactCount":2,"narration":"完成","nested":{"ok":true}}',
    "sequence": 7,
    "global_position": 19,
}
EXPECTED_CANONICAL_BYTES = (
    b'{"actorId":"agent:worker-1","causationId":"event-start-1",'
    b'"correlationId":"corr-1","eventId":"event-golden-1",'
    b'"eventType":"codec.golden.checked","globalPosition":19,'
    b'"idempotencyKey":"codec:event-1","payload":{"artifactCount":2,'
    b'"narration":"\xe5\xae\x8c\xe6\x88\x90","nested":{"ok":true}},'
    b'"schemaVersion":1,"sequence":7,"streamId":"session:alpha",'
    b'"timestamp":"2026-08-28T09:10:11.123456Z"}'
)
EXPECTED_DIGEST = "9a2f7b7b20dbd320adf2a3ba8818fa9a5cb15668b6d0c68567290ac74bc55e54"
RAW_COLUMNS = (
    "global_position",
    "stream_id",
    "sequence",
    "event_id",
    "event_type",
    "actor_id",
    "timestamp",
    "payload_json",
    "correlation_id",
    "causation_id",
    "idempotency_key",
)
RAW_VALUES: dict[str, object] = {
    "global_position": BASE_VALUES["global_position"],
    "stream_id": BASE_VALUES["stream_id"],
    "sequence": BASE_VALUES["sequence"],
    "event_id": BASE_VALUES["event_id"],
    "event_type": BASE_VALUES["event_type"],
    "actor_id": BASE_VALUES["actor_id"],
    "timestamp": BASE_VALUES["timestamp"],
    "payload_json": BASE_VALUES["payload_json"],
    "correlation_id": BASE_VALUES["correlation_id"],
    "causation_id": BASE_VALUES["causation_id"],
    "idempotency_key": BASE_VALUES["idempotency_key"],
}


def envelope(**overrides: object) -> codec._StoredEventEnvelopeV1:
    values = {**BASE_VALUES, **overrides}
    return codec._stored_event_envelope_from_values(**values)


def raw_row(
    *,
    columns: tuple[str, ...] = RAW_COLUMNS,
    **overrides: object,
) -> sqlite3.Row:
    values = {**RAW_VALUES, **overrides}
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        select_list = ", ".join(f"? AS {column}" for column in columns)
        row = connection.execute(
            f"SELECT {select_list}",
            tuple(values[column] for column in columns),
        ).fetchone()
        assert type(row) is sqlite3.Row
        return row
    finally:
        connection.close()


def test_canonical_body_and_domain_separated_digest_are_exact() -> None:
    value = envelope()

    assert value.to_dict() == {
        "schemaVersion": 1,
        "eventId": "event-golden-1",
        "streamId": "session:alpha",
        "eventType": "codec.golden.checked",
        "actorId": "agent:worker-1",
        "timestamp": "2026-08-28T09:10:11.123456Z",
        "correlationId": "corr-1",
        "causationId": "event-start-1",
        "idempotencyKey": "codec:event-1",
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
        ("event_id", "event-golden-2"),
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
        (
            "payload_json",
            '{"artifactCount":2,"narration":"已完成","nested":{"ok":true}}',
        ),
        (
            "payload_json",
            '{"artifactCount":2,"narration":"完成","nested":{"ok":false}}',
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
        ("event_id", b"event-golden-1", codec.StoredEventEnvelopeTypeError),
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
        envelope(event_id=StringSubclass("event-golden-1"))
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


def test_raw_sqlite_row_and_frozen_values_have_one_digest() -> None:
    from_values = envelope()
    from_storage = codec._stored_event_envelope_from_raw_row(raw_row())

    assert from_storage is not from_values
    assert from_storage.to_dict() == from_values.to_dict()
    assert from_storage.canonical_bytes() == EXPECTED_CANONICAL_BYTES
    assert from_storage.digest() == EXPECTED_DIGEST


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("global_position", 20),
        ("stream_id", "session:beta"),
        ("sequence", 8),
        ("event_id", "event-golden-2"),
        ("event_type", "task.status.changed"),
        ("actor_id", "agent:worker-2"),
        ("timestamp", "2026-08-28T09:10:12.123456Z"),
        (
            "payload_json",
            '{"artifactCount":3,"narration":"完成","nested":{"ok":true}}',
        ),
        ("correlation_id", None),
        ("causation_id", None),
        ("idempotency_key", None),
    ),
)
def test_every_raw_sqlite_column_is_covered_by_the_digest(
    field: str,
    replacement: object,
) -> None:
    changed = codec._stored_event_envelope_from_raw_row(
        raw_row(**{field: replacement})
    )

    assert changed.digest() != EXPECTED_DIGEST


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("global_position", "19"),
        ("stream_id", b"session:alpha"),
        ("sequence", 7.0),
        ("event_id", b"event-golden-1"),
        ("event_type", 1),
        ("actor_id", b"agent:worker-1"),
        ("timestamp", b"2026-08-28T09:10:11.123456Z"),
        ("payload_json", b"{}"),
        ("correlation_id", 1),
        ("causation_id", b"event-start-1"),
        ("idempotency_key", 1),
    ),
)
def test_raw_row_rejects_wrong_sqlite_storage_classes(
    field: str,
    invalid: object,
) -> None:
    with pytest.raises(codec.StoredEventEnvelopeTypeError):
        codec._stored_event_envelope_from_raw_row(raw_row(**{field: invalid}))


@pytest.mark.parametrize(
    "columns",
    (
        RAW_COLUMNS[:-1],
        RAW_COLUMNS + ("future",),
        tuple(reversed(RAW_COLUMNS)),
        (RAW_COLUMNS[1], RAW_COLUMNS[0], *RAW_COLUMNS[2:]),
    ),
)
def test_raw_row_requires_the_exact_closed_column_projection(
    columns: tuple[str, ...],
) -> None:
    overrides = {"future": "future-value"}
    with pytest.raises(codec.StoredEventEnvelopeCanonicalError, match="columns"):
        codec._stored_event_envelope_from_raw_row(
            raw_row(columns=columns, **overrides)
        )


@pytest.mark.parametrize("invalid", ({}, tuple(RAW_VALUES.values()), object()))
def test_raw_row_rejects_adapters_and_lookalikes(invalid: object) -> None:
    with pytest.raises(codec.StoredEventEnvelopeTypeError, match="exact sqlite3.Row"):
        codec._stored_event_envelope_from_raw_row(invalid)


@pytest.mark.parametrize(
    "payload_json",
    (
        "",
        "not-json",
        "[]",
        "null",
        "true",
        "1",
        '{"a":1,"a":2}',
        '{"a": 1}',
        '{ "a":1}',
        '{"b":2,"a":1}',
        '{"text":"\\u5b8c\\u6210"}',
        '{"text":"line\\nfeed"}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":-0}',
        '{"value":01}',
    ),
)
def test_noncanonical_or_non_object_payload_storage_is_rejected(payload_json: str) -> None:
    with pytest.raises(codec.StoredEventEnvelopeError):
        codec._stored_event_envelope_from_raw_row(raw_row(payload_json=payload_json))


def test_payload_text_keys_and_structural_bounds_match_the_store_contract() -> None:
    invalid_payloads = (
        '{"%s":true}' % ("k" * 513),
        '{"value":"%s"}' % ("x" * 65_537),
        '{"value":%s}' % (1 << 4_096),
        '{"e\\u0301":true}',
        '{"value":"e\\u0301"}',
    )
    for payload_json in invalid_payloads:
        with pytest.raises(codec.StoredEventEnvelopeError):
            codec._stored_event_envelope_from_raw_row(
                raw_row(payload_json=payload_json)
            )


def test_payload_depth_and_node_bounds_fail_closed() -> None:
    too_deep: object = "leaf"
    for _ in range(65):
        too_deep = {"child": too_deep}
    deep_json = json.dumps(
        too_deep,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    wide_json = '{"items":[' + ",".join("null" for _ in range(10_000)) + "]}"

    for payload_json in (deep_json, wide_json):
        with pytest.raises(codec.StoredEventEnvelopeError):
            codec._stored_event_envelope_from_raw_row(
                raw_row(payload_json=payload_json)
            )


def test_valid_canonical_json_edge_values_remain_exact() -> None:
    payload_json = (
        '{"emptyKey":{"":true},"emptyString":"","float":1.5,'
        '"negativeZero":-0.0,"space":"ordinary space"}'
    )
    value = codec._stored_event_envelope_from_raw_row(
        raw_row(payload_json=payload_json)
    )

    assert value.to_dict()["payload"] == {
        "emptyKey": {"": True},
        "emptyString": "",
        "float": 1.5,
        "negativeZero": -0.0,
        "space": "ordinary space",
    }
