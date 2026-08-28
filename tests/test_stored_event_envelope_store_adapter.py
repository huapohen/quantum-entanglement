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
    SQLiteEventStore,
    _ResultEventWriteContractError,
)
from tests.test_invocation_result_evidence import valid_evidence
from tests.test_invocation_result_terminal_transition import valid_transition

T0 = "2026-08-28T13:14:15.123456Z"
DEFAULT_RESULT_IDEMPOTENCY_KEY = "accept:invocation-m3-store"
PRE_M3_STORE_WILDCARD_NAMES = frozenset(
    {
        "Any",
        "AttemptStatus",
        "CANONICAL_ORCHESTRATOR_ACTOR_ID",
        "Callable",
        "CancelledError",
        "ConcurrencyError",
        "ContextManager",
        "Dict",
        "DomainEvent",
        "EventStoreIntegrityError",
        "EventStoreJsonError",
        "EventStoreJsonTooLargeError",
        "EventStoreJsonTypeError",
        "EventStoreJsonValueError",
        "EventStoreLifecycleError",
        "EventStorePoisonedError",
        "InboxAppendResult",
        "InboxReceipt",
        "InvocationAdmissionCommitAmbiguityError",
        "InvocationAdmissionConflictError",
        "InvocationAdmissionResult",
        "InvocationAdmissionTransactionError",
        "InvocationAttempt",
        "InvocationIntegrityError",
        "InvocationJob",
        "InvocationJobSpec",
        "InvocationStartClaimed",
        "InvocationStartCommitAmbiguityError",
        "InvocationStartConflictError",
        "InvocationStartEvidenceV2",
        "InvocationStartObserved",
        "InvocationStartReceipt",
        "InvocationStartTransactionError",
        "InvocationStatus",
        "Iterable",
        "Iterator",
        "List",
        "Mapping",
        "MappingProxyType",
        "NoReturn",
        "Optional",
        "OutboxAmbiguity",
        "OutboxAmbiguityPageItem",
        "OutboxMessage",
        "OutboxPageItem",
        "OutboxStatus",
        "ReservedResultEventError",
        "SQLiteEventStore",
        "SQLiteInvocationAttemptStore",
        "ScopedInvocationStartClaimedV3",
        "ScopedInvocationStartEvidenceV3",
        "ScopedInvocationStartObservedV3",
        "ScopedInvocationStartReceiptV3",
        "ScopedTaskInvocationAdmissionRequestV2",
        "StoredEvent",
        "StoredOutboxMessage",
        "SupportsIndex",
        "TASK_INVOCATION_STARTED_EVENT_TYPE",
        "TaskInvocationAdmissionRequest",
        "Tuple",
        "TypeVar",
        "Union",
        "annotations",
        "apply_sqlite_migrations",
        "cast",
        "contextmanager",
        "dataclass",
        "datetime",
        "hashlib",
        "json",
        "math",
        "new_id",
        "os",
        "secrets",
        "sqlite3",
        "threading",
        "timedelta",
        "timezone",
        "traceback_module",
        "unicodedata",
        "utc_now",
        "wraps",
    }
)


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
    expected_version: int | None = 0,
    expected_global_position: int | None = None,
) -> tuple[StoredEvent, bool, codec._StoredEventEnvelopeV1]:
    snapshot = SQLiteEventStore._snapshot_event(store, candidate)
    with store._transaction() as connection:
        return SQLiteEventStore._insert_with_verified_envelope_in_transaction(
            store,
            connection,
            snapshot,
            expected_version,
            expected_global_position,
        )


def durable_rows(store: SQLiteEventStore) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row) for row in store._connection.execute("SELECT * FROM events").fetchall())


def event_store_traceback_locals(error: BaseException) -> str:
    """Render store-owned locals retained by one returned exception graph."""

    pending = [error]
    seen: set[int] = set()
    values: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            if traceback.tb_frame.f_code.co_filename == store_module.__file__:
                values.extend(
                    f"{name}={value!r}" for name, value in traceback.tb_frame.f_locals.items()
                )
            traceback = traceback.tb_next
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(values)


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
        replace(
            valid_result,
            event_id="event-result-without-correlation",
            correlation_id=None,
        ),
        replace(
            valid_result,
            event_id="event-result-without-causation",
            causation_id=None,
        ),
    )

    for candidate in invalid:
        with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
            statements: list[str] = []
            store._connection.set_trace_callback(statements.append)
            try:
                with pytest.raises(_ResultEventWriteContractError):
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
                with pytest.raises(_ResultEventWriteContractError):
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
        with pytest.raises(EventStoreIntegrityError, match="verification failed"):
            append_verified(store, event())
        assert durable_rows(store) == before

        changed = event(
            event_id="event-changed",
            payload=result_payload(resultRef="result:changed"),
        )
        with pytest.raises(EventStoreIntegrityError, match="verification failed"):
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


def test_trigger_cannot_relocate_the_original_and_verify_a_replacement_row() -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store._connection.execute(
            """
            CREATE TRIGGER replace_verified_row AFTER INSERT ON events
            BEGIN
                UPDATE events
                SET global_position = NEW.global_position + 100,
                    stream_id = 'session:relocated',
                    sequence = NEW.sequence + 100,
                    event_id = 'event-relocated',
                    actor_id = 'actor:relocated',
                    idempotency_key = 'accept:relocated'
                WHERE global_position = NEW.global_position;
                INSERT INTO events (
                    global_position, stream_id, sequence, event_id, event_type,
                    actor_id, timestamp, payload_json, correlation_id,
                    causation_id, idempotency_key
                ) VALUES (
                    NEW.global_position, NEW.stream_id, NEW.sequence, NEW.event_id,
                    NEW.event_type, NEW.actor_id, NEW.timestamp, NEW.payload_json,
                    NEW.correlation_id, NEW.causation_id, NEW.idempotency_key
                );
            END
            """
        )

        with pytest.raises(EventStoreIntegrityError, match="verification failed"):
            append_verified(store, event())

        assert durable_rows(store) == ()
        assert tuple(store._connection.execute("SELECT * FROM sqlite_sequence")) == ()


def test_before_trigger_cannot_update_an_old_row_and_ignore_the_insert() -> None:
    seed = DomainEvent(
        stream_id="session:seed",
        event_type="ordinary.seed",
        payload={"seed": True},
        actor_id="seed",
        event_id="event-seed",
        timestamp=T0,
        idempotency_key="seed:1",
    )
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store.append(seed, expected_version=0)
        before = durable_rows(store)
        store._connection.execute(
            """
            CREATE TRIGGER replace_old_row BEFORE INSERT ON events
            BEGIN
                UPDATE events
                SET stream_id = NEW.stream_id,
                    sequence = NEW.sequence,
                    event_id = NEW.event_id,
                    event_type = NEW.event_type,
                    actor_id = NEW.actor_id,
                    timestamp = NEW.timestamp,
                    payload_json = NEW.payload_json,
                    correlation_id = NEW.correlation_id,
                    causation_id = NEW.causation_id,
                    idempotency_key = NEW.idempotency_key
                WHERE global_position = 1;
                SELECT RAISE(IGNORE);
            END
            """
        )

        with pytest.raises(EventStoreIntegrityError, match="verification failed"):
            append_verified(store, event())

        assert durable_rows(store) == before


def test_trigger_cannot_append_an_unverified_extra_event_row() -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store._connection.execute(
            """
            CREATE TRIGGER append_extra_event AFTER INSERT ON events
            BEGIN
                INSERT INTO events (
                    stream_id, sequence, event_id, event_type, actor_id, timestamp,
                    payload_json, correlation_id, causation_id, idempotency_key
                ) VALUES (
                    'session:extra', 1, 'event-extra', 'ordinary.extra', 'actor:extra',
                    NEW.timestamp, '{"extra":true}', NULL, NULL, 'extra:1'
                );
            END
            """
        )

        with pytest.raises(EventStoreIntegrityError, match="verification failed"):
            append_verified(store, event())

        assert durable_rows(store) == ()
        assert tuple(store._connection.execute("SELECT * FROM sqlite_sequence")) == ()
        store._connection.execute("DROP TRIGGER append_extra_event")
        stored, inserted, _verified = append_verified(store, event())
        assert inserted is True
        assert stored.sequence == 1
        assert stored.global_position == 1


def test_verified_insert_rejects_every_trigger_side_effect() -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store._connection.execute("CREATE TABLE event_insert_audit (event_id TEXT NOT NULL)")
        store._connection.execute(
            """
            CREATE TRIGGER audit_event_insert AFTER INSERT ON events
            BEGIN
                INSERT INTO event_insert_audit (event_id) VALUES (NEW.event_id);
            END
            """
        )

        with pytest.raises(EventStoreIntegrityError, match="verification failed"):
            append_verified(store, event())

        assert durable_rows(store) == ()
        assert tuple(store._connection.execute("SELECT * FROM event_insert_audit")) == ()


@pytest.mark.parametrize(
    "column",
    (
        "stream_id",
        "event_id",
        "event_type",
        "actor_id",
        "timestamp",
        "payload_json",
        "correlation_id",
        "causation_id",
        "idempotency_key",
    ),
)
def test_verifier_rejects_blob_storage_for_every_text_column(column: str) -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        snapshot = SQLiteEventStore._snapshot_event(store, event())
        with pytest.raises(EventStoreIntegrityError, match="readback is invalid"):
            with store._transaction() as connection:
                stored, inserted = SQLiteEventStore._append_in_transaction(
                    store,
                    connection,
                    snapshot,
                    0,
                )
                assert inserted is True
                connection.execute(
                    f"UPDATE events SET {column} = CAST({column} AS BLOB) "
                    "WHERE global_position = ?",
                    (stored.global_position,),
                )
                SQLiteEventStore._verify_stored_event_envelope_in_transaction(
                    store,
                    connection,
                    snapshot,
                    stored,
                )

        assert durable_rows(store) == ()


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
            with pytest.raises(EventStoreIntegrityError, match="verification failed"):
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


def test_contract_failures_detach_every_payload_bearing_store_frame() -> None:
    event_canary = "event-contract-private-canary"
    value_canary = "payload-contract-private-canary"
    digest_canary = "1234567890abcdef" * 4
    payload = result_payload(resultRef=value_canary, leaseTokenDigest=digest_canary)
    payload["unsupportedCanary"] = value_canary
    candidate = event(event_id=event_canary, payload=payload)

    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        with pytest.raises(_ResultEventWriteContractError) as raised:
            append_verified(store, candidate)
        assert durable_rows(store) == ()

    assert raised.value.args == ("private result event append requires an exact typed payload",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    graph = event_store_traceback_locals(raised.value)
    for canary in (event_canary, value_canary, digest_canary):
        assert canary not in repr(raised.value) + str(raised.value)
        assert canary not in graph
    for payload_frame_name in ("snapshot=", "frozen=", "event=", "payload="):
        assert payload_frame_name not in graph


def test_readback_failures_detach_every_payload_bearing_store_frame() -> None:
    event_canary = "event-private-canary"
    value_canary = "credential-private-canary"
    digest_canary = "abcdef0123456789" * 4
    candidate = event(
        event_id=event_canary,
        payload=result_payload(resultRef=value_canary, leaseTokenDigest=digest_canary),
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

    assert raised.value.args == ("stored event envelope verification failed",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    rendered = repr(raised.value) + str(raised.value)
    graph = event_store_traceback_locals(raised.value)
    for canary in (event_canary, value_canary, digest_canary):
        assert canary not in rendered
        assert canary not in graph
    for payload_frame_name in ("snapshot=", "frozen=", "event=", "payload="):
        assert payload_frame_name not in graph


@pytest.mark.parametrize(
    ("expected_version", "expected_global_position"),
    ((1, None), (0, 0)),
)
def test_concurrency_failures_detach_every_payload_bearing_store_frame(
    expected_version: int,
    expected_global_position: int | None,
) -> None:
    event_canary = "event-concurrency-private-canary"
    value_canary = "payload-concurrency-private-canary"
    digest_canary = "fedcba9876543210" * 4
    candidate = event(
        event_id=event_canary,
        payload=result_payload(resultRef=value_canary, leaseTokenDigest=digest_canary),
    )
    seed = DomainEvent(
        stream_id="session:seed",
        event_type="ordinary.seed",
        payload={"seed": True},
        actor_id="actor:seed",
        event_id="event-seed",
        timestamp=T0,
        idempotency_key="seed:1",
    )
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        store.append(seed, expected_version=0)
        before = durable_rows(store)
        with pytest.raises(store_module.ConcurrencyError) as raised:
            append_verified(
                store,
                candidate,
                expected_version=expected_version,
                expected_global_position=expected_global_position,
            )
        assert durable_rows(store) == before

    assert raised.value.args == ("verified stored event append concurrency conflict",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    graph = event_store_traceback_locals(raised.value)
    for canary in (event_canary, value_canary, digest_canary):
        assert canary not in repr(raised.value) + str(raised.value)
        assert canary not in graph
    for payload_frame_name in ("snapshot=", "frozen=", "event=", "payload="):
        assert payload_frame_name not in graph


@pytest.mark.parametrize("kind", ("contract", "integrity", "concurrency"))
def test_clean_adapter_errors_drop_an_active_exception_context(kind: str) -> None:
    canary = "active-exception-context-private-canary"
    try:
        raise ValueError(canary)
    except ValueError:
        with pytest.raises(BaseException) as raised:
            store_module._raise_clean_stored_event_envelope_error(kind)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canary not in event_store_traceback_locals(raised.value)


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


def test_insert_adapter_rejects_missing_or_foreign_transaction_before_insert() -> None:
    with (
        SQLiteEventStore(":memory:", clock=lambda: T0) as store,
        SQLiteEventStore(":memory:", clock=lambda: T0) as other,
    ):
        snapshot = SQLiteEventStore._snapshot_event(store, event())
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        try:
            with pytest.raises(RuntimeError, match="open transaction"):
                SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                    store,
                    store._connection,
                    snapshot,
                    0,
                )
            with other._transaction() as foreign:
                with pytest.raises(RuntimeError, match="owning connection"):
                    SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                        store,
                        foreign,
                        snapshot,
                        0,
                    )
        finally:
            store._connection.set_trace_callback(None)

        assert durable_rows(store) == ()
        assert durable_rows(other) == ()
        assert not any(
            statement.lstrip().upper().startswith("INSERT INTO EVENTS") for statement in statements
        )


def test_insert_adapter_does_not_mislabel_a_closed_owning_connection() -> None:
    store = SQLiteEventStore(":memory:", clock=lambda: T0)
    snapshot = SQLiteEventStore._snapshot_event(store, event())
    connection = store._connection
    store.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        SQLiteEventStore._insert_with_verified_envelope_in_transaction(
            store,
            connection,
            snapshot,
            0,
        )


@pytest.mark.parametrize(
    ("expected_version", "expected_global_position"),
    ((True, None), (0, True)),
)
def test_insert_adapter_rejects_boolean_coordinates_before_insert(
    expected_version: int,
    expected_global_position: int | None,
) -> None:
    with SQLiteEventStore(":memory:", clock=lambda: T0) as store:
        snapshot = SQLiteEventStore._snapshot_event(store, event())
        with store._transaction() as connection:
            with pytest.raises(TypeError, match="must be an integer"):
                SQLiteEventStore._insert_with_verified_envelope_in_transaction(
                    store,
                    connection,
                    snapshot,
                    expected_version,
                    expected_global_position,
                )
        assert durable_rows(store) == ()


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


def test_store_wildcard_surface_remains_pre_m3_compatible() -> None:
    namespace: dict[str, object] = {}
    exec("from quantum_entanglement.store import *", namespace)
    assert set(namespace) - {"__builtins__"} == PRE_M3_STORE_WILDCARD_NAMES
