from __future__ import annotations

import inspect
import json
import sqlite3
from dataclasses import replace
from typing import Any

import pytest

import quantum_entanglement.store as store_module
from quantum_entanglement import _stored_event_envelope_codec as codec
from quantum_entanglement.events import DomainEvent, StoredEvent
from quantum_entanglement.invocation_execution import CANONICAL_ORCHESTRATOR_ACTOR_ID
from quantum_entanglement.invocation_results import (
    TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
    TASK_STATUS_CHANGED_EVENT_TYPE,
    ScopedInvocationResultEvidenceV2,
    ScopedInvocationResultTerminalTransitionV2,
)
from quantum_entanglement.store import (
    EventStoreIntegrityError,
    ResultEventWriteContractError,
    SQLiteEventStore,
)
from tests.test_invocation_result_evidence import valid_evidence
from tests.test_invocation_result_terminal_transition import valid_transition

T0 = "2026-08-28T13:14:15.123456Z"
DEFAULT_RESULT_IDEMPOTENCY_KEY = "accept:invocation-m3-store"


def result_payload(**changes: object) -> dict[str, object]:
    payload = valid_evidence(
        session_id="m3-store",
        accepted_at=T0,
        acceptance_idempotency_key=DEFAULT_RESULT_IDEMPOTENCY_KEY,
        artifact_count=0,
    ).to_dict()
    payload.update(changes)
    return payload


def event(
    *,
    event_id: str = "event-m3-store",
    payload: dict[str, object] | None = None,
    idempotency_key: str = DEFAULT_RESULT_IDEMPOTENCY_KEY,
) -> DomainEvent:
    event_payload = result_payload(acceptanceIdempotencyKey=idempotency_key)
    if payload is not None:
        event_payload = payload
    return DomainEvent(
        stream_id="session:m3-store",
        event_type=TASK_INVOCATION_RESULT_ACCEPTED_EVENT_TYPE,
        payload=event_payload,
        actor_id=CANONICAL_ORCHESTRATOR_ACTOR_ID,
        event_id=event_id,
        timestamp=T0,
        correlation_id="correlation-m3-store",
        causation_id="event-m3-start",
        idempotency_key=idempotency_key,
    )


def terminal_event(
    *,
    event_id: str = "event-m3-terminal",
    session_id: str | None = None,
    correlation_id: str | None = None,
    result_event_id: str | None = None,
) -> DomainEvent:
    transition = valid_transition()
    transition = replace(
        transition,
        session_id=transition.session_id if session_id is None else session_id,
        correlation_id=(transition.correlation_id if correlation_id is None else correlation_id),
        result_event_id=(
            transition.result_event_id if result_event_id is None else result_event_id
        ),
    )
    return DomainEvent(
        stream_id=transition.stream_id,
        event_type=TASK_STATUS_CHANGED_EVENT_TYPE,
        payload=ScopedInvocationResultTerminalTransitionV2.to_dict(transition),
        actor_id=transition.actor_id,
        event_id=event_id,
        timestamp=T0,
        correlation_id=transition.correlation_id,
        causation_id=transition.causation_id,
        idempotency_key=transition.idempotency_key,
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


def test_private_adapter_accepts_only_exact_typed_result_payload_bytes() -> None:
    candidates = (event(), terminal_event())
    typed_payloads = (
        ScopedInvocationResultEvidenceV2.from_dict(candidates[0].payload),
        ScopedInvocationResultTerminalTransitionV2.from_dict(candidates[1].payload),
    )
    typed_bytes = (
        ScopedInvocationResultEvidenceV2.canonical_bytes(typed_payloads[0]),  # type: ignore[arg-type]
        ScopedInvocationResultTerminalTransitionV2.canonical_bytes(typed_payloads[1]),  # type: ignore[arg-type]
    )

    for candidate, expected_bytes in zip(candidates, typed_bytes):
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            stored, inserted, verified = append_verified(store, candidate)
            row = store._connection.execute(
                "SELECT payload_json FROM events WHERE global_position = ?",
                (stored.global_position,),
            ).fetchone()

        assert inserted is True
        assert type(row["payload_json"]) is str
        assert row["payload_json"].encode("utf-8") == expected_bytes
        assert verified.to_dict()["payload"] == candidate.payload


def test_non_typed_or_misbound_reserved_snapshots_fail_before_insert() -> None:
    valid_result = event()
    valid_terminal = terminal_event()
    invalid = (
        DomainEvent(
            stream_id=valid_result.stream_id,
            event_type="ordinary.event",
            payload=valid_result.payload,
            actor_id=valid_result.actor_id,
            event_id="event-ordinary",
            timestamp=valid_result.timestamp,
            correlation_id=valid_result.correlation_id,
            causation_id=valid_result.causation_id,
            idempotency_key=valid_result.idempotency_key,
        ),
        event(payload={**valid_result.payload, "future": True}),
        DomainEvent(
            stream_id=valid_terminal.stream_id,
            event_type=TASK_STATUS_CHANGED_EVENT_TYPE,
            payload=valid_result.payload,
            actor_id=valid_terminal.actor_id,
            event_id="event-wrong-terminal-payload",
            timestamp=T0,
            correlation_id=valid_terminal.correlation_id,
            causation_id=valid_terminal.causation_id,
            idempotency_key=valid_terminal.idempotency_key,
        ),
        DomainEvent(
            stream_id="session:wrong",
            event_type=valid_result.event_type,
            payload=valid_result.payload,
            actor_id=valid_result.actor_id,
            event_id="event-wrong-result-binding",
            timestamp=valid_result.timestamp,
            correlation_id=valid_result.correlation_id,
            causation_id=valid_result.causation_id,
            idempotency_key=valid_result.idempotency_key,
        ),
        DomainEvent(
            stream_id=valid_terminal.stream_id,
            event_type=valid_terminal.event_type,
            payload=valid_terminal.payload,
            actor_id=valid_terminal.actor_id,
            event_id="event-wrong-terminal-binding",
            timestamp=valid_terminal.timestamp,
            correlation_id=valid_terminal.correlation_id,
            causation_id=valid_terminal.causation_id,
            idempotency_key="task-status:wrong:1",
        ),
    )

    for candidate in invalid:
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            statements: list[str] = []
            store._connection.set_trace_callback(statements.append)
            try:
                with pytest.raises(ResultEventWriteContractError):
                    append_verified(store, candidate)
            finally:
                store._connection.set_trace_callback(None)
            assert durable_rows(store) == ()
            assert not any(
                statement.lstrip().upper().startswith("INSERT INTO EVENTS")
                for statement in statements
            )


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
    original_payload = result_payload(resultRef="result:original")
    candidate = event(payload=original_payload)
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
    expected_payload_json = json.dumps(
        original_payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert tuple(row) == ("event-m3-store", expected_payload_json)
    assert verified.to_dict()["eventId"] == "event-m3-store"
    assert verified.to_dict()["payload"] == original_payload


def test_caller_and_snapshot_payload_mutation_cannot_change_frozen_insert_bytes() -> None:
    caller_payload = result_payload(resultRef="result:immutable")
    candidate = event(payload=caller_payload)
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        snapshot = SQLiteEventStore._snapshot_event(store, candidate)
        caller_payload["resultRef"] = "result:caller-mutated"
        object.__setattr__(candidate, "event_id", "event-caller-mutated")
        assert type(snapshot.event.payload) is dict
        snapshot.event.payload["resultRef"] = "result:snapshot-payload-mutated"

        with store._transaction() as connection:
            stored, inserted, verified = (
                SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                    store,
                    connection,
                    snapshot,
                    0,
                )
            )
        raw = store._connection.execute(
            "SELECT event_id, payload_json FROM events WHERE global_position = 1"
        ).fetchone()

    assert inserted is True
    assert stored.event.event_id == "event-m3-store"
    assert verified.to_dict()["payload"]["resultRef"] == "result:immutable"
    assert raw["event_id"] == "event-m3-store"
    assert json.loads(raw["payload_json"])["resultRef"] == "result:immutable"


def test_reflective_payload_byte_drift_fails_before_insert() -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        snapshot = SQLiteEventStore._snapshot_event(store, event())
        object.__setattr__(snapshot, "payload_json", " " + snapshot.payload_json)
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        try:
            with store._transaction() as connection:
                with pytest.raises(ResultEventWriteContractError):
                    SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                        store,
                        connection,
                        snapshot,
                        0,
                    )
        finally:
            store._connection.set_trace_callback(None)

        assert durable_rows(store) == ()
        assert not any(
            statement.lstrip().upper().startswith("INSERT INTO EVENTS") for statement in statements
        )


def test_instance_method_shadow_cannot_replace_private_verified_composition() -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store._freeze_typed_result_event_write_snapshot = lambda _snapshot: object()  # type: ignore[method-assign]
        store._append_in_transaction = lambda *_args, **_kwargs: (object(), False)  # type: ignore[method-assign]
        store._verify_stored_event_envelope_in_transaction = lambda *_args: object()  # type: ignore[method-assign]

        stored, inserted, verified = append_verified(store, event())

    assert inserted is True
    assert type(stored) is StoredEvent
    assert type(verified) is codec._StoredEventEnvelopeV1
    assert stored.global_position == 1


def test_idempotent_replay_never_mints_a_verified_insert() -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        first, inserted, _first_envelope = append_verified(store, event())
        before = durable_rows(store)

        assert inserted is True
        with pytest.raises(EventStoreIntegrityError, match="requires a fresh row"):
            append_verified(store, event())
        assert durable_rows(store) == before

        changed = event(
            event_id="event-changed",
            payload=result_payload(resultRef="result:changed"),
        )
        with pytest.raises(EventStoreIntegrityError, match="requires a fresh row"):
            append_verified(store, changed)
        assert durable_rows(store) == before
        assert first.global_position == 1


@pytest.mark.parametrize(
    "trigger_body",
    (
        "UPDATE events SET global_position = NEW.global_position + 1 "
        "WHERE global_position = NEW.global_position;",
        "UPDATE events SET stream_id = 'session:drifted' "
        "WHERE global_position = NEW.global_position;",
        "UPDATE events SET sequence = NEW.sequence + 1 "
        "WHERE global_position = NEW.global_position;",
        "UPDATE events SET event_id = 'event-drifted' WHERE global_position = NEW.global_position;",
        "UPDATE events SET event_type = 'task.status.changed' "
        "WHERE global_position = NEW.global_position;",
        "UPDATE events SET actor_id = 'actor:drifted' WHERE global_position = NEW.global_position;",
        "UPDATE events SET timestamp = '2026-08-28T13:14:16.123456Z' "
        "WHERE global_position = NEW.global_position;",
        "UPDATE events SET payload_json = ' ' || payload_json "
        "WHERE global_position = NEW.global_position;",
        "UPDATE events SET correlation_id = NULL WHERE global_position = NEW.global_position;",
        "UPDATE events SET causation_id = NULL WHERE global_position = NEW.global_position;",
        "UPDATE events SET idempotency_key = NULL WHERE global_position = NEW.global_position;",
        "UPDATE events SET payload_json = CAST(payload_json AS BLOB) "
        "WHERE global_position = NEW.global_position;",
        "UPDATE events SET sequence = CAST(sequence AS BLOB) "
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
            event(event_id="event-after-rollback", idempotency_key="accept:after-rollback"),
        )
        assert inserted is True
        assert stored.sequence == 1
        assert stored.global_position == 1


def test_two_verified_rows_bind_real_consecutive_coordinates() -> None:
    result = event()
    terminal = terminal_event(
        event_id="event-m3-terminal-pair",
        session_id="m3-store",
        correlation_id=result.correlation_id,
        result_event_id=result.event_id,
    )
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        with store._transaction() as connection:
            first = SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                store,
                connection,
                SQLiteEventStore._snapshot_event(store, result),
                0,
            )
            second = SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                store,
                connection,
                SQLiteEventStore._snapshot_event(store, terminal),
                1,
            )

    assert first[0].sequence == 1
    assert first[0].global_position == 1
    assert second[0].sequence == 2
    assert second[0].global_position == 2
    assert first[2].digest() != second[2].digest()


def test_second_row_verification_failure_rolls_back_the_pair() -> None:
    result = event()
    terminal = terminal_event(
        event_id="event-m3-terminal-pair",
        session_id="m3-store",
        correlation_id=result.correlation_id,
        result_event_id=result.event_id,
    )
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store._connection.execute(
            """
            CREATE TRIGGER drift_terminal AFTER INSERT ON events
            WHEN NEW.event_id = 'event-m3-terminal-pair'
            BEGIN
                UPDATE events SET causation_id = NULL
                WHERE global_position = NEW.global_position;
            END
            """
        )

        with pytest.raises(EventStoreIntegrityError):
            with store._transaction() as connection:
                SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                    store,
                    connection,
                    SQLiteEventStore._snapshot_event(store, result),
                    0,
                )
                SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                    store,
                    connection,
                    SQLiteEventStore._snapshot_event(store, terminal),
                    1,
                )

        assert durable_rows(store) == ()


def test_non_sqlite_row_readback_is_rejected_and_rolled_back() -> None:
    def mapping_row(
        cursor: sqlite3.Cursor,
        row: tuple[object, ...],
    ) -> dict[str, object]:
        return {description[0]: value for description, value in zip(cursor.description, row)}

    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store._connection.row_factory = mapping_row
        try:
            with pytest.raises(EventStoreIntegrityError, match="readback is invalid"):
                append_verified(store, event())
        finally:
            store._connection.row_factory = sqlite3.Row

        assert durable_rows(store) == ()
        stored, inserted, _verified = append_verified(store, event())
        assert inserted is True
        assert stored.global_position == 1


def test_raw_recompute_control_signal_rolls_back_and_store_remains_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(_row: object) -> codec._StoredEventEnvelopeV1:
        raise KeyboardInterrupt("private readback interruption")

    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        monkeypatch.setattr(store_module, "_stored_event_envelope_from_raw_row", interrupt)
        with pytest.raises(KeyboardInterrupt, match="private readback interruption"):
            append_verified(store, event())
        assert durable_rows(store) == ()

        monkeypatch.undo()
        stored, inserted, _verified = append_verified(store, event())
        assert inserted is True
        assert stored.global_position == 1


def test_readback_failures_do_not_disclose_event_or_payload_canaries() -> None:
    event_canary = "event-private-canary"
    value_canary = "credential-private-canary"
    candidate = event(
        event_id=event_canary,
        payload=result_payload(resultRef=value_canary),
    )
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store._connection.execute(
            """
            CREATE TRIGGER drift_canary AFTER INSERT ON events
            BEGIN
                UPDATE events SET actor_id = 'actor:drifted'
                WHERE global_position = NEW.global_position;
            END
            """
        )
        with pytest.raises(EventStoreIntegrityError) as raised:
            append_verified(store, candidate)

    rendered = repr(raised.value) + str(raised.value)
    assert event_canary not in rendered
    assert value_canary not in rendered
    assert "resultRef" not in rendered


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
