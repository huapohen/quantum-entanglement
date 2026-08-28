from __future__ import annotations

import json
from dataclasses import replace

import pytest

from quantum_entanglement import _stored_event_envelope_codec as codec
from quantum_entanglement.events import DomainEvent
from quantum_entanglement.store import SQLiteEventStore, _EventWriteSnapshot

T0 = "2026-08-28T12:34:56.123456Z"


def event() -> DomainEvent:
    return DomainEvent(
        stream_id="session:m3",
        event_type="task.invocation.result.accepted",
        payload={"nested": {"ok": True}, "resultRef": "result:m3", "unicode": "完成"},
        actor_id="orchestrator",
        event_id="event-m3-result",
        timestamp=T0,
        correlation_id="correlation-m3",
        causation_id="event-m3-start",
        idempotency_key="result-event:invocation-m3",
    )


def envelope(
    store: SQLiteEventStore,
    candidate: DomainEvent,
) -> codec._StoredEventEnvelopeV1:
    snapshot = SQLiteEventStore._snapshot_event(store, candidate)
    return SQLiteEventStore._stored_event_envelope_from_write_snapshot(
        snapshot,
        sequence=7,
        global_position=19,
    )


def test_write_snapshot_adapter_uses_exact_frozen_insert_values() -> None:
    candidate = event()
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        value = envelope(store, candidate)

    expected_payload_json = json.dumps(
        dict(candidate.payload),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    expected = codec._stored_event_envelope_from_values(
        event_id=candidate.event_id,
        stream_id=candidate.stream_id,
        event_type=candidate.event_type,
        actor_id=candidate.actor_id,
        timestamp=candidate.timestamp,
        correlation_id=candidate.correlation_id,
        causation_id=candidate.causation_id,
        idempotency_key=candidate.idempotency_key,
        payload_json=expected_payload_json,
        sequence=7,
        global_position=19,
    )

    assert value.to_dict() == expected.to_dict()
    assert value.canonical_bytes() == expected.canonical_bytes()
    assert value.digest() == expected.digest()


def test_caller_mutation_after_snapshot_cannot_change_the_envelope() -> None:
    candidate = event()
    original_payload = candidate.payload
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        snapshot = SQLiteEventStore._snapshot_event(store, candidate)
        assert type(original_payload) is dict
        original_payload["resultRef"] = "result:mutated"
        original_payload["late"] = True
        object.__setattr__(candidate, "event_id", "event-mutated")

        value = SQLiteEventStore._stored_event_envelope_from_write_snapshot(
            snapshot,
            sequence=1,
            global_position=1,
        )

    assert value.to_dict()["eventId"] == "event-m3-result"
    assert value.to_dict()["payload"] == {
        "nested": {"ok": True},
        "resultRef": "result:m3",
        "unicode": "完成",
    }


def test_adapter_rejects_snapshot_and_domain_event_lookalikes() -> None:
    class SnapshotSubclass(_EventWriteSnapshot):
        pass

    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        snapshot = SQLiteEventStore._snapshot_event(store, event())
        subclass = SnapshotSubclass(snapshot.event, snapshot.payload_json)
        malformed = replace(snapshot)
        object.__setattr__(malformed, "event", object())

        for candidate in (subclass, object()):
            with pytest.raises(TypeError, match="exact class"):
                SQLiteEventStore._stored_event_envelope_from_write_snapshot(
                    candidate,  # type: ignore[arg-type]
                    sequence=1,
                    global_position=1,
                )
        with pytest.raises(TypeError, match="exact DomainEvent"):
            SQLiteEventStore._stored_event_envelope_from_write_snapshot(
                malformed,
                sequence=1,
                global_position=1,
            )


@pytest.mark.parametrize(
    ("sequence", "global_position"),
    ((True, 1), (1, False), (0, 1), (1, 0), (1 << 63, 1), (1, 1 << 63)),
)
def test_adapter_retains_exact_positive_sqlite_coordinate_contract(
    sequence: object,
    global_position: object,
) -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        snapshot = SQLiteEventStore._snapshot_event(store, event())
        with pytest.raises(codec.StoredEventEnvelopeError):
            SQLiteEventStore._stored_event_envelope_from_write_snapshot(
                snapshot,
                sequence=sequence,  # type: ignore[arg-type]
                global_position=global_position,  # type: ignore[arg-type]
            )


def test_instance_shadow_cannot_replace_the_class_qualified_adapter() -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        snapshot = SQLiteEventStore._snapshot_event(store, event())
        store._stored_event_envelope_from_write_snapshot = lambda *_args, **_kwargs: object()  # type: ignore[method-assign]

        value = SQLiteEventStore._stored_event_envelope_from_write_snapshot(
            snapshot,
            sequence=1,
            global_position=1,
        )

    assert type(value) is codec._StoredEventEnvelopeV1
    assert repr(value) == "_StoredEventEnvelopeV1(<capability-free>)"
