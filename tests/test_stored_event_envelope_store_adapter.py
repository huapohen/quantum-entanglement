from __future__ import annotations

import inspect
from typing import Any

import pytest

from quantum_entanglement import _stored_event_envelope_codec as codec
from quantum_entanglement.events import DomainEvent, StoredEvent
from quantum_entanglement.store import EventStoreIntegrityError, SQLiteEventStore

T0 = "2026-08-28T13:14:15.123456Z"


def event(
    *,
    event_id: str = "event-m3-store",
    payload: dict[str, object] | None = None,
    idempotency_key: str = "result-event:invocation-m3-store",
) -> DomainEvent:
    return DomainEvent(
        stream_id="session:m3-store",
        event_type="task.invocation.result.accepted",
        payload={"resultRef": "result:m3-store"} if payload is None else payload,
        actor_id="orchestrator",
        event_id=event_id,
        timestamp=T0,
        correlation_id="correlation-m3-store",
        causation_id="event-m3-start",
        idempotency_key=idempotency_key,
    )


def append_verified(
    store: SQLiteEventStore,
    candidate: DomainEvent,
    *,
    expected_version: int = 0,
) -> tuple[StoredEvent, bool, codec._StoredEventEnvelopeV1]:
    snapshot = SQLiteEventStore._snapshot_event(store, candidate)
    with store._transaction() as connection:
        return SQLiteEventStore._insert_with_verified_envelope_in_transaction(
            store,
            connection,
            snapshot,
            expected_version,
        )


def durable_rows(store: SQLiteEventStore) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row) for row in store._connection.execute("SELECT * FROM events").fetchall())


def test_private_adapter_verifies_exact_raw_row_before_commit() -> None:
    statements: list[str] = []
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store._connection.set_trace_callback(statements.append)
        try:
            stored, inserted, verified = append_verified(store, event())
        finally:
            store._connection.set_trace_callback(None)
        row = store._connection.execute(
            """
            SELECT
                global_position, stream_id, sequence, event_id, event_type, actor_id,
                timestamp, payload_json, correlation_id, causation_id, idempotency_key
            FROM events WHERE global_position = ?
            """,
            (stored.global_position,),
        ).fetchone()
        raw = codec._stored_event_envelope_from_raw_row(row)

    assert inserted is True
    assert verified is not raw
    assert verified.to_dict() == raw.to_dict()
    assert verified.canonical_bytes() == raw.canonical_bytes()
    assert verified.digest() == raw.digest()
    normalized = [statement.lstrip().upper() for statement in statements]
    begin = next(
        index for index, statement in enumerate(normalized) if statement.startswith("BEGIN")
    )
    insert = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("INSERT INTO EVENTS")
    )
    readback = next(
        index
        for index, statement in enumerate(normalized)
        if statement.startswith("SELECT\n") and "GLOBAL_POSITION" in statement
    )
    commit = next(
        index for index, statement in enumerate(normalized) if statement.startswith("COMMIT")
    )
    assert begin < insert < readback < commit


def test_private_adapter_never_uses_domain_or_read_model_serializers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("read-model serialization must not run")

    monkeypatch.setattr(DomainEvent, "to_dict", forbidden)
    monkeypatch.setattr(StoredEvent, "to_dict", forbidden)
    monkeypatch.setattr(SQLiteEventStore, "_row_to_event", forbidden)

    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        stored, inserted, verified = append_verified(store, event())

    assert inserted is True
    assert stored.global_position == 1
    assert verified.to_dict()["eventId"] == "event-m3-store"


def test_hidden_frozen_bind_snapshot_closes_mid_insert_caller_mutation() -> None:
    candidate = event(payload={"resultRef": "result:original"})
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        snapshot = SQLiteEventStore._snapshot_event(store, candidate)

        def mutate_visible_snapshots(_statement: str) -> None:
            object.__setattr__(candidate, "event_id", "event-caller-mutated")
            object.__setattr__(snapshot.event, "event_id", "event-snapshot-mutated")
            assert type(snapshot.event.payload) is dict
            snapshot.event.payload["resultRef"] = "result:snapshot-mutated"

        with store._transaction() as connection:
            store._connection.set_trace_callback(mutate_visible_snapshots)
            try:
                stored, inserted, verified = (
                    SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                        store,
                        connection,
                        snapshot,
                        0,
                    )
                )
            finally:
                store._connection.set_trace_callback(None)

        row = store._connection.execute(
            "SELECT event_id, payload_json FROM events WHERE global_position = 1"
        ).fetchone()

    assert inserted is True
    assert stored.event.event_id == "event-m3-store"
    assert tuple(row) == ("event-m3-store", '{"resultRef":"result:original"}')
    assert verified.to_dict()["eventId"] == "event-m3-store"
    assert verified.to_dict()["payload"] == {"resultRef": "result:original"}


def test_idempotent_replay_never_mints_a_verified_insert() -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        first, inserted, _first_envelope = append_verified(store, event())
        before = durable_rows(store)

        assert inserted is True
        with pytest.raises(EventStoreIntegrityError, match="requires a fresh row"):
            append_verified(store, event())
        assert durable_rows(store) == before

        changed = event(event_id="event-changed", payload={"resultRef": "result:changed"})
        with pytest.raises(EventStoreIntegrityError, match="requires a fresh row"):
            append_verified(store, changed)
        assert durable_rows(store) == before
        assert first.global_position == 1


@pytest.mark.parametrize(
    "trigger_body",
    (
        "UPDATE events SET event_id = 'event-drifted' WHERE global_position = NEW.global_position;",
        'UPDATE events SET payload_json = \'{"resultRef": "result:m3-store"}\' '
        "WHERE global_position = NEW.global_position;",
        "UPDATE events SET payload_json = CAST(payload_json AS BLOB) "
        "WHERE global_position = NEW.global_position;",
        "DELETE FROM events WHERE global_position = NEW.global_position;",
    ),
)
def test_raw_row_drift_or_missing_row_rolls_back(trigger_body: str) -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store._connection.execute(
            "CREATE TRIGGER drift_event AFTER INSERT ON events BEGIN " + trigger_body + " END"
        )
        before_sequence = tuple(store._connection.execute("SELECT * FROM sqlite_sequence"))

        with pytest.raises(EventStoreIntegrityError):
            append_verified(store, event())

        assert durable_rows(store) == ()
        assert tuple(store._connection.execute("SELECT * FROM sqlite_sequence")) == before_sequence
        store._connection.execute("DROP TRIGGER drift_event")
        stored, inserted, _verified = append_verified(
            store,
            event(event_id="event-after-rollback", idempotency_key="result-event:after-rollback"),
        )
        assert inserted is True
        assert stored.sequence == 1
        assert stored.global_position == 1


def test_verifier_rejects_foreign_or_closed_transaction_before_select() -> None:
    with (
        SQLiteEventStore(":memory:", clock=lambda: T0) as store,
        SQLiteEventStore(":memory:", clock=lambda: T0) as other,
    ):
        snapshot = SQLiteEventStore._snapshot_event(store, event())
        fake_stored = StoredEvent(snapshot.event, 1, 1)
        with pytest.raises(RuntimeError, match="open transaction"):
            SQLiteEventStore._verify_stored_event_envelope_in_transaction(
                store,
                store._connection,
                snapshot,
                fake_stored,
            )
        with other._transaction() as foreign:
            with pytest.raises(RuntimeError, match="owning connection"):
                SQLiteEventStore._verify_stored_event_envelope_in_transaction(
                    store,
                    foreign,
                    snapshot,
                    fake_stored,
                )


def test_verifier_uses_fixed_projection_and_private_composition_only() -> None:
    source = inspect.getsource(SQLiteEventStore._verify_stored_event_envelope_in_transaction)
    assert "SELECT *" not in source.upper()
    for column in (
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
    ):
        assert source.count(column) >= 1
    public_signatures: dict[str, inspect.Signature] = {
        name: inspect.signature(member)
        for name, member in vars(SQLiteEventStore).items()
        if callable(member) and not name.startswith("_")
    }
    for name, signature in public_signatures.items():
        assert {"trusted", "allow_reserved", "connection", "transaction"}.isdisjoint(
            signature.parameters
        ), name
    assert inspect.getsource(SQLiteEventStore).count("INSERT INTO events (") == 2
